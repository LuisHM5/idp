# Modos de extracción

Esta guía responde dos preguntas distintas:

1. ¿Qué estrategia se **solicitó** para una carga?
2. ¿Qué estrategia **terminó ejecutándose** y si intervino IA generativa?

La decisión global es: **`catalog` debe permanecer como el valor por default de
`extractionMode`**. No es una quinta forma de extraer: en PDF/imagen busca si el
tipo declaró un mapper; si existe lo ejecuta y, si no existe, usa IA para todos
los campos. Hoy `PEDIMENTO_ERP` usa código determinístico estricto,
`CERTIFICADO_POLIZA`, las dos constancias de situación fiscal y `CURP` usan
perfiles por código con fallback parcial/completo, los XML usan XPath y los demás PDF/imágenes siguen usando el modelo hasta que
tengan un mapper confiable.

## Control independiente: formato de respuesta de IA

`aiResponseFormat` no es otro modo de extracción. Cuando el árbol anterior sí
necesita IA, decide cuánto JSON debe generar el modelo:

| Valor | Efecto |
|---|---|
| `compact` | **Default.** La IA devuelve únicamente rutas con `[valor, confianza]` y matrices posicionales; Python arma el resultado final. |
| `legacy` | Restaura el JSON anidado `{value, confidence}` anterior. Útil para A/B y rollback. |

Precedencia: request autorizado → `ai_response_format` del catálogo →
`AI_RESPONSE_FORMAT` global → `compact`. En producción el request necesita el
permiso por app `AllowAiResponseFormatOverride`. Un dato vacío mal expresado
(`null`, `[null]`, una fila sin sus celdas opcionales finales) se repara sin
volver a llamar; una rama que sigue ilegible se pide otra vez en legacy SOLA
(`retriedPaths`), y únicamente un payload que no sigue el protocolo repite la
partición completa. Campos ausentes o inválidos semánticamente no se
regeneran.

Medido el 2026-10-14: `minimax.minimax-m2.5` siempre razona antes de llamar a la
herramienta (bloque `reasoningContent`), y Bedrock ignora `reasoning_effort`,
`thinking` y `reasoning` en `additionalModelRequestFields`. En una constancia
~50% de sus tokens de salida fue razonamiento. La única palanca es pedirle
menos: mapper por código y columnas `$common` calculadas por el pipeline. `documents[].aiResponseFormat` explica qué pasó y
`aiResponseSavings` separa el contrafactual de los tokens reales de `aiUsage`.

## 1. Nombre y efecto de cada modo

`extractionMode` es un override de la carga, no una afirmación de lo que
ocurrió. Sólo modifica la extracción de campos de PDF/JPG/PNG en el pipeline de
expedientes.

| Valor técnico | Nombre operativo | Efecto directo | IA de extracción | Uso recomendado |
|---|---|---|---:|---|
| `catalog` | **USA EL FLUJO CONFIGURADO: MAPPER SI EXISTE; SI NO, IA** | Busca `deterministic_extraction` en el tipo. Si no existe, manda todos los campos al modelo. Si existe, ejecuta el mapper y su `fallback` decide si los faltantes van a IA o a revisión. | Depende de esa configuración | **Default global y producción** |
| `model` | **IA PARA TODOS LOS CAMPOS** | Omite el mapper y manda el schema completo al modelo. Reproduce el comportamiento anterior. | Sí | Contrafactual de calidad, latencia y costo en dev |
| `hybrid` | **CÓDIGO PRIMERO; IA PARA FALTANTES** | Conserva lo resuelto por el mapper y manda únicamente las ramas faltantes al modelo. | Sólo si quedan faltantes | Migración/calibración de un mapper |
| `deterministic` | **SÓLO CÓDIGO; FALTANTES A REVISIÓN** | No abre el modelo para extraer campos. Lo no resuelto queda vacío y la validación decide si requiere revisión. | No, en la extracción de campos | Prueba estricta del mapper y tipos maduros |

La lectura rápida es:

```text
catalog       = mapper configurado → código; sin mapper → IA completa
model         = IA completa
hybrid        = código + IA sólo si falta algo
deterministic = código únicamente
```

### Flujo exacto de `catalog`

`catalog` **no intenta construir un mapper automáticamente** y tampoco
significa “sin IA”. Ejecuta este árbol de decisión:

```text
archivo
├─ XML/XLSX/XLSM/CSV
│  └─ extracción por código (XPath/celda/columna); extractionMode no interviene
└─ PDF/JPG/PNG
   └─ ¿el tipo declara deterministic_extraction?
      ├─ NO
      │  └─ MODEL: todos los campos van a IA
      └─ SÍ
         └─ primero ejecuta el mapper por código
            ├─ ¿declara perfiles?
            │  ├─ perfil único → confía en mapper y revisa sus expected_fields
            │  └─ desconocido/empate → profile_fallback: IA completa o revisión
            ├─ fallback: review
            │  └─ DETERMINISTIC: no abre IA; faltantes van a revisión
            └─ fallback: missing_fields
               ├─ mapper resolvió lo esperado → DETERMINISTIC: no abre IA
               └─ quedaron esperados faltantes → HYBRID: sólo esos campos van a IA
```

Ejemplos actuales:

| Carga con `extractionMode: catalog` | Flujo que toma | Efecto |
|---|---|---|
| `PEDIMENTO_ERP` PDF | Tiene mapper + `fallback: review` | Código solamente; faltantes a revisión; cero IA de extracción |
| `CERTIFICADO_POLIZA` de perfil conocido | Tiene mapper + perfil único | Código; sólo un campo esperado faltante abre IA parcial |
| `CERTIFICADO_POLIZA` con formato nuevo o ambiguo | Ningún perfil gana de forma única | IA completa por `profile_fallback: full_model`; no confía en matches tentativos |
| `ACTA_NACIMIENTO` PDF | No tiene mapper | Todos los campos van al modelo |
| `PEDIMENTO_ERP_XML` XML | Ruta estructurada por extensión | XPath por código; `extractionMode` no interviene |

Si el request omite `extractionMode` y el schema del tipo tampoco declara
`deterministic_extraction`, el resultado es inequívoco:

```text
requested = catalog
effective = model
strategy  = model
```

Es decir, **la ausencia de configuración conserva la extracción tradicional
con IA**; nunca activa código determinístico por accidente.

Reglas de seguridad:

- `hybrid` y `deterministic` sólo pueden aplicarse si el tipo declara
  `deterministic_extraction`. En otro tipo la estrategia efectiva permanece
  `model` y el resultado reporta `reason: type_has_no_deterministic_mapping`.
  Esto evita entregar un documento vacío por un override incompatible.
- Los overrides distintos de `catalog` están habilitados sólo en dev. Producción
  los ignora y vuelve a `catalog`, igual que el override de `modelId`.
- Un error interno del mapper nunca degrada silenciosamente una carga normal:
  `catalog`/`hybrid` fallan abierto al modelo completo. En el override explícito
  `deterministic`, el objetivo es medir código solamente y los faltantes pasan a
  validación/revisión.

La resolución completa por familia queda así:

| Familia del tipo | `catalog` | `model` | `hybrid` | `deterministic` |
|---|---|---|---|---|
| PDF/imagen sin mapper (18 activos) | Modelo | Modelo | Modelo, con razón de incompatibilidad | Modelo, con razón de incompatibilidad |
| PDF/imagen con mapper (`PEDIMENTO_ERP`, `CERTIFICADO_POLIZA`, `CONSTANCIA_SITUACION_FISCAL[_PORTAL]`, `CURP`) | Política/perfil del catálogo | Modelo completo | Mapper + modelo sólo si faltan ramas | Sólo mapper |
| XML con XPath (7 activos) | Código XML | Código XML | Código XML | Código XML |
| XLSX/XLSM/CSV con `cell`/`column` (0 activos) | Código tabular | Código tabular | Código tabular | Código tabular |

En el modo híbrido de un tipo con mapper, `effective` permanece `hybrid`, pero
la `strategy` observada puede ser `deterministic` si el mapper resolvió todo y
por tanto no hubo ningún fallback.

## 2. Por qué `catalog` es el default correcto

La alternativa global `deterministic` no es viable: 18 tipos PDF/imagen todavía
no tienen mapper. `model` renunciaría al ahorro ya conseguido y `hybrid` tendría
una latencia p95 impredecible: basta un solo campo no resuelto para volver a
abrir una generación que puede producir miles de tokens.

`catalog` separa la decisión estable del API de la evolución de cada tipo:

- conserva compatibilidad para tipos que sólo tienen extracción por modelo;
- activa código puro únicamente donde el catálogo ya expresa cómo mapear cada
  campo y cuál es su fallback;
- permite cambiar un tipo de `model` a `hybrid` y luego a `deterministic` con
  una versión de catálogo y `pnpm sync:one`, sin cambiar a los consumidores;
- mantiene una salida auditable: el request dice `requested: catalog` y el
  documento dice si la estrategia efectiva fue `model`, `hybrid` o
  `deterministic`.

Por eso la recomendación operativa es:

```text
default global del API       = catalog
PEDIMENTO_ERP en su catálogo = deterministic_extraction + fallback: review
overrides de request         = experimentos controlados en dev
```

## 3. Estrategia efectiva de los 30 tipos activos

Inventario del catálogo generado al 2026-10-13. La columna “con `catalog`”
describe la extracción de campos; otras etapas posibles se explican en §4.

| `TYPE_ID` | Estado | Fuente | Estrategia con `catalog` |
|---|---|---|---|
| `ACTA_CONSTITUTIVA_PORTAL` | beta | PDF/imagen | Modelo |
| `ACTA_NACIMIENTO` | beta | PDF/imagen | Modelo |
| `CARTA_NOM35` | beta | PDF/imagen | Modelo + autenticidad según catálogo |
| `CEDULA_PROFESIONAL` | beta | PDF/imagen | Modelo |
| `CERTIFICADO_POLIZA` | estable | PDF/imagen | **Perfiles `EMP`/`FL`/`GMM`/`SV`: mapper; IA parcial si falta su contrato, IA completa si el perfil es desconocido/ambiguo** |
| `CFDI_GASTO_TERCERO_XML` | beta | XML | XPath, código puro |
| `CFDI_PAGO_DERECHOS_SANITARIOS_XML` | beta | XML | XPath, código puro |
| `COMPROBANTE_DOMICILIO` | beta | PDF/imagen | Modelo |
| `COMPROBANTE_ESTUDIOS` | beta | PDF/imagen | Modelo |
| `CONSTANCIA_NSS` | beta | PDF/imagen | Modelo |
| `CONSTANCIA_SITUACION_FISCAL` | estable | PDF/imagen | **Perfiles `SAT_PERSONA_MORAL`/`SAT_PERSONA_FISICA`: identificadores, fechas, domicilio numérico, actividades y regímenes por código; IA sólo para nombres, texto libre y obligaciones; formato desconocido → IA completa** |
| `CONSTANCIA_SITUACION_FISCAL_PORTAL` | beta | PDF/imagen | **Mismos perfiles SAT (+`PAIS=MEX`); constancia extranjera, W-9 o escaneo → IA completa** |
| `CONTRATO_SERVICIO_PORTAL` | beta | PDF/imagen | Modelo |
| `CURP` | beta | PDF/imagen | **Perfil `RENAPO_CONSTANCIA`: clave, folio, entidad, fecha y certificación por código; IA sólo para separar nombre y apellidos** |
| `ESTADO_CUENTA_PORTAL` | beta | PDF/imagen | Modelo + integridad según catálogo |
| `FACTURA_FLETE_EXTRANJERO` | estable | PDF/imagen | Modelo |
| `FACTURA_HONORARIOS` | estable | PDF/imagen | Modelo |
| `FACTURA_HONORARIOS_XML` | estable | XML | XPath, código puro |
| `FACTURA_LOGISTICA_XML` | beta | XML | XPath, código puro |
| `IDENTIFICACION` | beta | PDF/imagen | Modelo |
| `IDENTIFICACION_OFICIAL_PORTAL` | beta | PDF/imagen | Modelo |
| `INE` | estable | PDF/imagen | Modelo |
| `PASAPORTE` | beta | PDF/imagen | Modelo |
| `PEDIMENTO_ADUANAL` | estable | PDF/imagen | Modelo |
| `PEDIMENTO_ADUANAL_XML` | estable | XML | XPath, código puro |
| `PEDIMENTO_ERP` | estable | PDF/imagen | **Mapper determinístico; `fallback: review`** |
| `PEDIMENTO_ERP_XML` | beta | XML | XPath, código puro |
| `PEDIMENTO_GASTOS_ADUANALES_ERP` | beta | PDF/imagen | Modelo |
| `PEDIMENTO_GASTOS_ADUANALES_ERP_XML` | beta | XML | XPath, código puro |
| `REGISTRO_PUBLICO_COMERCIO_PORTAL` | beta | PDF/imagen | Modelo |

Totales actuales:

- 18 tipos PDF/imagen con extracción de campos por modelo;
- 7 tipos XML con XPath determinístico;
- 5 tipos PDF con mapper de texto: `PEDIMENTO_ERP` estricto, y
  `CERTIFICADO_POLIZA`, `CONSTANCIA_SITUACION_FISCAL[_PORTAL]` y `CURP` por
  perfiles con fallback;
- 0 tipos tabulares activos. La ruta XLSX/XLSM/CSV ya existe y será código puro
  mediante `cell`/`column` cuando se publique un catálogo que la use.

Los XML y tabulares se enrutan por la extensión real del archivo antes del
pipeline PDF; `extractionMode` no cambia esa ruta. Para ellos debe declararse un
único `documentType`, porque no se usa visión para elegir entre schemas.

## 4. “Extracción sin IA” no siempre significa “carga sin IA”

`deterministic` garantiza que el mapper de campos no abra Bedrock. Todavía
podrían existir otras etapas generativas si la forma de la carga las exige:

| Etapa | Cuándo puede usar IA | Cómo evitarla cuando aplica |
|---|---|---|
| Clasificación | No se declaró tipo o el flujo upstream no pudo hacer bypass | Declarar exactamente un `documentType` |
| Segmentación | Se deben distinguir varios tipos/segmentos y los marcadores no bastan | Declarar un solo tipo cuando el archivo realmente contiene uno |
| Localizador de páginas | Los marcadores fallan en un tipo con `expected_pages: "auto"` | Corregir/calibrar `page_selection` |
| Extracción de campos | Estrategia efectiva `model` o `hybrid` con faltantes | Mapper completo + estrategia determinística |
| Autenticidad/visión | El catálogo solicita una señal visual | Sólo se elimina quitando esa comprobación del contrato, no con `extractionMode` |
| OCR de Textract | Página escaneada o imagen | No es IA generativa, pero sí tiene tiempo y costo propios |

Para el camino sin IA generativa de `PEDIMENTO_ERP`, la carga debe declarar
`documentType=PEDIMENTO_ERP` y dejar `extractionMode=catalog` (o usar
`deterministic` en dev). La respuesta es la autoridad final: no se debe inferir
por la duración.

## 5. Cómo comprobar qué ocurrió

En la raíz del resultado:

```jsonc
{
  "aiExecuted": false,
  "aiUsage": {
    "executed": false,
    "stages": [],
    "inputTokens": 0,
    "outputTokens": 0,
    "images": 0,
    "tokenReportedStages": []
  },
  "deterministicSavings": {
    "estimated": true,
    "billedInputTokens": 0,
    "billedOutputTokens": 0,
    "estimatedInputTokensSaved": 20538,
    "estimatedOutputTokensSaved": 894
  }
}
```

- `aiExecuted` cubre cualquier etapa de modelo registrada para la ejecución,
  no sólo la extracción.
- `aiUsage.stages` indica cuáles etapas ejecutaron un modelo.
- Los totales sólo suman tokens que llegaron reportados al envelope;
  `tokenReportedStages` explicita esas etapas. Una etapa puede constar como
  ejecutada aunque su Lambda upstream no haya propagado sus tokens.
- Textract OCR no cuenta como IA generativa y se ve por separado en
  `pages.ocrPages` y en `tracking.timings.pageLoad`.
- `deterministicSavings` es un contrafactual estimado contra el schema completo;
  nunca se mezcla con tokens facturados.

En cada documento:

```jsonc
{
  "extractionMode": {
    "requested": "catalog",
    "effective": "deterministic"
  },
  "deterministicExtraction": {
    "strategy": "deterministic",
    "aiExecuted": false,
    "profile": {
      "id": "FL",
      "status": "matched",
      "scores": {"EMP": 0, "FL": 4, "GMM": 0, "SV": 0}
    },
    "resolvedFieldCount": 24,
    "fallbackFields": [],
    "tokenSavings": {}
  }
}
```

Las diferencias importantes son:

- `requested` = **qué pidió el cliente** después de aplicar la compuerta del
  ambiente. Puede ser `catalog`, `model`, `hybrid` o `deterministic`;
- `effective` = **qué política pudo aplicar el tipo**. Sólo puede ser `model`,
  `hybrid` o `deterministic`: `catalog` ya quedó resuelto en este punto;
- `strategy` = **qué ocurrió realmente en ese documento**. Puede ser
  `deterministic`, `hybrid` o `model` según los campos efectivamente resueltos;
- `fallbackFields` muestra exactamente qué ramas fueron al modelo.
- `profile.status` explica la decisión de layout: `matched` conserva el mapper;
  `unknown`/`ambiguous` aplican `profile_fallback`; `not_configured` indica un
  mapper sin perfiles. `profile.id` sólo existe cuando hubo ganador único.

### Tabla de interpretación de estados

| Salida | Significado operativo | Efecto |
|---|---|---|
| `requested: catalog` | El cliente no fuerza estrategia | Busca mapper: si existe lo ejecuta y respeta su fallback; si no existe usa IA completa |
| `requested: model` | El cliente pide el comportamiento anterior | Schema completo a IA |
| `requested: hybrid` | El cliente pide ahorrar sin aceptar faltantes | Código primero; IA para faltantes |
| `requested: deterministic` | El cliente prohíbe IA de extracción | Sólo código; faltantes a revisión |
| `effective: model` | El tipo terminará usando extracción completa por IA | Puede tener latencia/costo de generación |
| `effective: hybrid` | El tipo tiene mapper y permite fallback | IA sólo es posible para ramas faltantes |
| `effective: deterministic` | El tipo tiene mapper y no permite fallback a IA | Cero IA de extracción |
| `strategy: model` | No se resolvió nada por mapper o se forzó modelo | La extracción fue completa por IA |
| `strategy: hybrid` | Mapper y modelo aportaron campos | Hubo IA de extracción |
| `strategy: deterministic` | El mapper resolvió todo lo enviado | No hubo IA de extracción para ese documento |
| `aiExecuted: true` | Alguna etapa generativa registrada sí corrió | Consultar `aiUsage.stages` para saber cuál |
| `aiExecuted: false` | Ninguna etapa generativa registrada corrió | OCR Textract todavía pudo ejecutarse |
| `reason: type_has_no_deterministic_mapping` | Se pidió `hybrid`/`deterministic` a un tipo sin mapper | Se protegió la salida usando `model` |

## 6. Comandos de comparación

El caso normal no necesita flag:

```bash
pnpm enviar pedimento.pdf PEDIMENTO_ERP
```

En dev, el mismo archivo se puede medir contra las tres estrategias efectivas:

```bash
pnpm enviar pedimento.pdf PEDIMENTO_ERP --extraction-mode model
pnpm enviar pedimento.pdf PEDIMENTO_ERP --extraction-mode hybrid
pnpm enviar pedimento.pdf PEDIMENTO_ERP --extraction-mode deterministic
```

Para aislar el efecto del protocolo de respuesta sin cambiar archivo, tipo,
modelo ni estrategia, el comparador ejecuta una carga `compact` y otra
`legacy`, espera ambas y contrasta latencia, tokens y valores:

```bash
pnpm comparar:formato -- pedimento.pdf PEDIMENTO_ERP --extraction-mode model
```

Son dos procesamientos reales con costo. El override individual también queda
disponible para una sola prueba:

```bash
pnpm enviar pedimento.pdf PEDIMENTO_ERP --ai-response-format legacy
```

Para comparar correctamente hay que conservar el mismo archivo, tipo, modelo y
catálogo, y revisar al menos:

1. `validation.status` y diferencias de campos;
2. `tracking.timings.extraction` y `processingSeconds`;
3. `aiExecuted`, `aiUsage` y `deterministicSavings`;
4. `fallbackFields`, que explica una cola larga en modo híbrido.

## 7. Evidencia de la decisión en `PEDIMENTO_ERP`

En el pedimento real de 17 páginas medido el 2026-10-12, el fallback híbrido
abrió MiniMax sobre campos faltantes: la extracción tardó 160.75 s y consumió
38,804 tokens de entrada y 10,058 de salida. Con el catálogo 1.2.0 y
`fallback: review`, una ejecución desplegada posterior terminó en 2.47 s, con
0/0 tokens y `aiExecuted: false`. La mayor latencia no provenía del tamaño del
JSON final, sino de la generación no streaming del modelo.

Estas cifras prueban la decisión para este layout; no justifican activar un
mapper incompleto en los demás tipos. Cada nuevo mapper debe contrastarse
contra ground truth y observarse primero en `hybrid` antes de cambiar su
fallback de catálogo a `review`.

La anatomía completa del mapper de `PEDIMENTO_ERP` y la receta para implementar
otro formato fijo están en
[EXTRACCION_DETERMINISTICA_E_HIBRIDA.md](EXTRACCION_DETERMINISTICA_E_HIBRIDA.md).

