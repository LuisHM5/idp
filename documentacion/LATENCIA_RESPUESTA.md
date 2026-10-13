# Latencia de respuesta y OCR bajo demanda

**Estado:** propuesta documentada, todavía no implementada  
**Fecha del análisis:** 2026-10-13  
**Primer tipo candidato:** `CERTIFICADO_POLIZA`

## Resumen

Hoy `pdf_pages.load_pages` manda a Textract toda página que no tenga una capa
de texto utilizable **antes** de saber si el mapeo determinístico ya resolvió el
documento. Esto conserva un comportamiento fail-open, pero desperdicia tiempo y
costo en páginas que no aportan campos.

La mejora propuesta es hacer la adquisición de texto en dos pasadas:

1. leer gratuitamente el texto nativo de todas las páginas;
2. intentar el perfil y el mapeo determinístico;
3. ejecutar Textract únicamente si siguen faltando campos esperados;
4. después del OCR, volver a intentar código y mandar a IA compacta solamente
   lo que aún falte.

La estrategia será opt-in por catálogo. Los tipos que no la declaren
conservarán exactamente el OCR actual.

## Caso medido: `emp_cert.pdf`

Se compararon dos ejecuciones del mismo PDF, catálogo `CERTIFICADO_POLIZA` versión
`5.1.0`, modelo configurado `minimax.minimax-m2.5` y resultado `APROBADO`.
Ninguna corrida ejecutó IA: ambas reportaron `aiExecuted: false`, cero tokens de
entrada y cero tokens de salida.

| Medición | A · antes | B · después de la migración | Diferencia |
|---|---:|---:|---:|
| `processingSeconds` | 2.05 s | 3.33 s | **+1.28 s** |
| `pageLoad` | 1.968 s | 3.247 s | **+1.279 s** |
| `extraction` | 23.8 ms | 20.9 ms | -2.9 ms |
| `authenticity` | 61.8 ms | 61.9 ms | +0.1 ms |
| `validation` | 0.8 ms | 0.7 ms | -0.1 ms |
| Step Functions completa | 8.83 s | 6.51 s | **-2.32 s** |
| PUT de S3 → resultado construido | 31.72 s¹ | 8.28 s | **-23.44 s (-74%)** |
| PUT de S3 → inicio de Step Functions | 23.77 s | 2.69 s | **-21.08 s** |

¹ La versión A aún no publicaba `uploadToResultSeconds`; se reconstruyó con el
`eventTime` del evento S3 y `processingCompletedAt` del resultado.

## Análisis de latencia de respuesta

`processingSeconds` no es la latencia que percibe el consumidor. Ese reloj
empieza dentro del pipeline documental, después de S3, SQS, el consumidor, el
arranque de Step Functions y la preparación de la Lambda. Para analizar la
respuesta completa se distinguen estos límites:

| Métrica | Inicio | Fin | Uso correcto |
|---|---|---|---|
| `processingSeconds` | inicio de `process_pdf` | envelope construido | rendimiento del motor documental; no es un SLA extremo a extremo |
| `uploadToResultSeconds` | PUT terminado y evento S3 emitido | envelope construido | latencia server-side del resultado; aún no incluye los últimos milisegundos de persistencia ni el GET |
| `uploadToCompletionSeconds` | PUT terminado | workflow `FINISHED`/`FAILED` | incluye persistencia final, movimiento del archivo y evento terminal |
| Tiempo observado por cliente | antes del PUT o del GET | JSON recibido | añade red, subida y hasta 250/500 ms de espera del long poll |

`registeredAt` no debe usarse como inicio del procesamiento: corresponde al
momento en que se solicitó la URL prefirmada y el usuario podría tardar minutos
en efectuar el PUT.

### Línea de tiempo medida

| Hito UTC | A · antes | B · después |
|---|---|---|
| S3 terminó el PUT (`eventTime`) | 16:18:02.585 | 18:32:26.555 |
| Consumidor SQS inició | no se publicaba | 18:32:28.407 |
| Consumidor solicitó el workflow | no se publicaba | 18:32:28.886 |
| Step Functions inició | 16:18:26.359 | 18:32:29.247 |
| Pipeline documental inició | 16:18:32.252 | 18:32:31.504 |
| Resultado fue construido | 16:18:34.306 | 18:32:34.834 |
| Step Functions terminó | 16:18:35.191 | 18:32:35.757 |

De esos timestamps se obtiene el recorrido comparable:

| Tramo | A · antes | B · después | Lectura |
|---|---:|---:|---|
| PUT → inicio de Step Functions | 23.774 s | 2.692 s | la migración eliminó 21.082 s de espera de entrada |
| Step Functions → inicio del pipeline | 5.893 s | 2.257 s | la ruta `enrichment-only` eliminó los pasos upstream |
| Pipeline documental | 2.055 s | 3.330 s | regresión local de 1.276 s por la variación de Textract |
| Resultado → fin del workflow | 0.885 s | 0.923 s | persistencia/movimiento/evento prácticamente sin cambio |
| **PUT → resultado construido** | **31.721 s** | **8.279 s** | **74% menos tiempo** |
| **PUT → workflow terminado** | **32.606 s** | **9.202 s** | **72% menos tiempo** |

La ejecución B permite además abrir los primeros 8.279 segundos:

```text
1.852 s  S3/SQS hasta que inicia el consumidor
0.479 s  consumidor: tracking, metadata, consulta de ruta y StartExecution
2.617 s  solicitud del workflow hasta el inicio real del pipeline
3.330 s  pipeline documental, de los cuales 3.247 s son pageLoad
-------
8.278 s  PUT → resultado construido
```

La suma puede variar algunas milésimas por redondeo. La conclusión es que el
nuevo enrutamiento funciona: el servidor construye la respuesta 23.44
segundos antes. El aumento aislado de `processingSeconds` no revierte esa
mejora; expone la variabilidad de una llamada a Textract que todavía ocurre.

### Qué tiempo observa `pnpm enviar`

`pnpm enviar` registra el documento, hace el PUT a la URL prefirmada y devuelve
el `documentId`; deliberadamente **no espera el resultado documental**. El
comando que espera es:

```bash
pnpm resultado -- --id <documentId> --wait
```

El GET con `waitSeconds` consulta DynamoDB cada 250 ms durante las primeras ocho
iteraciones y después cada 500 ms. Por eso el tiempo visto en terminal puede ser
ligeramente mayor que `uploadToResultSeconds`: añade hasta un intervalo de
consulta, la ejecución del Query Lambda y la transferencia HTTP. Cuando se
requiera un SLA del servicio debe reportarse `uploadToResultSeconds` y
`uploadToCompletionSeconds`; el cronómetro del cliente se conserva como una
medición distinta de experiencia de usuario.

### Lectura correcta del comparativo

La migración sí mejoró el tiempo extremo a extremo: eliminó la espera larga de
entrada y dos pasos upstream cuyos resultados no se utilizaban. El aparente
retroceso de `2.05 s` a `3.33 s` pertenece exclusivamente a `pageLoad`.

Las dos invocaciones fueron cold starts comparables:

| Lambda de enriquecimiento | A | B |
|---|---:|---:|
| `Init Duration` | 364 ms | 330 ms |
| Duración de la invocación | 2.885 s | 4.371 s |

El cold start no explica la diferencia. En ambas corridas `pages` informó una
página nativa y una página OCR. La única operación externa dentro de
`pageLoad`, además de leer localmente el PDF, fue una llamada síncrona a
Textract. La variación de esa llamada explica prácticamente los 1.28 segundos.

### Por qué ese OCR no aportó valor

El PDF tiene dos páginas:

- la página 1 posee texto nativo y contiene los nueve campos esperados por el
  perfil `EMP`;
- la página 2 es una hoja sin datos, únicamente con el logotipo de Banorte, y
  no tiene capa de texto.

El perfil `EMP` ya declara `"pages": "first"`. Aun así, el pipeline actual
manda primero la página 2 a Textract y solo después ejecuta el perfil. Los nueve
campos se resolvieron desde la página 1 con `DETERMINISTIC_TEXT`; Textract no
aportó ningún valor.

El registro de costos confirma, para A y B:

| Servicio | A | B |
|---|---:|---:|
| Bedrock | USD 0 · 0 tokens | USD 0 · 0 tokens |
| Textract | 1 página · USD 0.001500 | 1 página · USD 0.001500 |
| Costo total registrado | USD 0.001570 | USD 0.001607 |

En B, Textract representa aproximadamente 93% del costo registrado. La pequeña
diferencia total entre A y B es cómputo Lambda causado por la mayor latencia del
mismo OCR.

## Configuración propuesta

La intención debe ser obvia en el catálogo:

```json
{
  "text_acquisition": {
    "ocr_mode": "only_if_deterministic_incomplete",
    "ocr_page_scope": "profile_pages_then_remaining"
  }
}
```

| Valor | Efecto exacto |
|---|---|
| `all_pages_without_native_text` | Comportamiento actual y default global: Textract procesa de inmediato toda página sin texto nativo. |
| `only_if_deterministic_incomplete` | Primero intenta código con texto nativo. No usa Textract si el perfil coincide y todos sus `expected_fields` quedaron resueltos. |
| `profile_pages_then_remaining` | Si falta información, aplica OCR primero a las páginas declaradas por el perfil; solo amplía al resto si la extracción continúa incompleta. |

`CERTIFICADO_POLIZA` usaría el segundo modo. Ningún otro tipo cambia de
comportamiento por omisión.

## Flujo propuesto

```text
PDF
 └─ leer texto nativo de todas las páginas
     └─ tipo único declarado + política determinística opt-in
         └─ detectar perfil con el texto disponible
             ├─ perfil coincide y expected_fields completos
             │   └─ terminar por código; Textract 0, IA 0
             └─ perfil desconocido o campos incompletos
                 └─ OCR de páginas del perfil
                     └─ reintentar mapeo determinístico
                         ├─ completo → terminar; IA 0
                         └─ incompleto → OCR restante necesario
                             └─ IA compacta solo para campos faltantes
```

El atajo solo se permite cuando hay un tipo único declarado y el catálogo
demuestra que el resultado está completo. Se conserva el comportamiento actual
en `AUTO`, múltiples tipos, perfil no reconocido, configuración inválida o
cualquier condición incierta. La página omitida del OCR sigue perteneciendo al
documento y los bytes originales siguen disponibles para integridad y
autenticidad.

## Cambios técnicos previstos

1. Separar en `pdf_pages.py` la lectura nativa y la aplicación posterior de OCR,
   sin duplicar el ordenamiento de bloques de Textract.
2. Añadir en `expediente.py` un preflight determinístico solo para tipos opt-in;
   reutilizar el resultado en la extracción definitiva para no mapear dos veces.
3. Resolver páginas iniciales desde `profiles[].pages`; si el preflight falla,
   expandir de forma fail-open.
4. Mantener `pageLoad` para compatibilidad y agregar métricas desglosadas:

   ```json
   {
     "timings": {
       "pageLoad": 120.0,
       "pdfParse": 35.0,
       "nativeText": 70.0,
       "textractOcr": 0.0
     },
     "pages": {
       "ocrPages": 0,
       "ocrPagesSkipped": 1,
       "ocrSkipReason": "deterministic_expected_fields_complete"
     }
   }
   ```

5. Registrar `TextractOcr.pages = 0` y costo cero cuando el OCR se difiera y
   finalmente no sea necesario.

## Resultado esperado para el ejemplo

Para `emp_cert.pdf`, sin prometer la latencia de un servicio externo:

- conservar los mismos nueve campos determinísticos y estado `APROBADO`;
- mantener `aiExecuted: false` y cero tokens;
- bajar `ocrPages` de 1 a 0;
- eliminar USD 0.001500 de Textract;
- bajar `pageLoad` de 2-3.25 segundos a decenas o pocos cientos de
  milisegundos;
- llevar `processingSeconds` cerca de 0.1-0.4 segundos en condiciones normales.

Esto no garantiza `uploadToResultSeconds <= 0.5 s`: en B, la cola por sí sola
consumió 1.85 segundos y la orquestación previa 2.62 segundos. Lograr 0.5
segundos extremo a extremo requeriría otro contrato síncrono sin SQS, Step
Functions, cold starts ni Textract; no forma parte de esta mejora.

## Verificación y criterios de aceptación

1. Prueba unitaria: perfil completo con una segunda página sin texto; Textract
   no debe ser invocado.
2. Prueba unitaria: falta un `expected_field`; Textract debe ejecutarse y el
   campo recuperado debe combinarse con los valores determinísticos.
3. Prueba unitaria: perfil no reconocido; debe conservarse el fallback completo.
4. Regresión sobre los cuatro formatos `EMP`, `FL`, `GMM` y `SV`: mismos campos,
   fuentes, validación y estado que antes.
5. E2E desplegado: `emp_cert.pdf` debe reportar `ocrPages: 0`, cero costo
   Textract y cero IA.
6. Benchmark mínimo de 10 repeticiones: comparar mediana y p95 de
   `processingSeconds`, `pageLoad` y `uploadToResultSeconds`; una sola corrida
   no sirve para evaluar la variación de Textract.
7. Meta inicial para este fixture: mediana de `processingSeconds <= 0.5 s`, sin
   convertirla en promesa de latencia extremo a extremo.

## Fuera de alcance

- eliminar SQS o Step Functions;
- crear un endpoint alterno `/documents/fast`;
- cambiar el modelo de Bedrock;
- saltar OCR en tipos que no lo autoricen explícitamente;
- inferir que una página es irrelevante solo porque parece visualmente vacía.

