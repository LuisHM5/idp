# Campos aduanales ausentes en `factura-pedimento.xml`

Fecha de revisión: 2026-08-12.

## Resumen

El archivo de referencia `data/xmlsx/factura-pedimento.xml` **no es un XML de
pedimento aduanal**. Su elemento raíz es `cfdi:Comprobante`, versión 4.0, y su
contenido corresponde a una factura CFDI: comprobante, emisor, receptor,
conceptos, impuestos y Timbre Fiscal Digital.

Por esta razón, `catalogs/base/aduanas/PEDIMENTO_ADUANAL_XML.json` extrae exhaustivamente
el CFDI disponible, pero no puede producir los campos aduanales que sí declara
`catalogs/base/aduanas/PEDIMENTO_ADUANAL.json` para el pedimento PDF. Un `xpath` solamente
puede recuperar nodos o atributos existentes; no debe inferir ni inventar los
datos faltantes.

La revisión automática del archivo confirmó que el catálogo XML cubre los
110 atributos presentes, contando las repeticiones de conceptos e impuestos.
Esto significa **100 % de cobertura del archivo recibido**, no 100 % de la
información de un pedimento.

## Campos del pedimento que no existen en este XML

### Identificación y régimen aduanal

| Campo de `PEDIMENTO_ADUANAL` | Situación en el CFDI |
|---|---|
| `NUMERO_PEDIMENTO` | Ausente. El XML no contiene un número de pedimento. |
| `NUMERO_PEDIMENTO_NORMALIZADO` | No puede calcularse porque falta el número fuente. |
| `PATENTE` | Ausente. |
| `NUMERO_CONSECUTIVO` | Ausente. |
| `ANIO_PEDIMENTO` | Ausente. No debe derivarse de la fecha del CFDI. |
| `ADUANA_DESPACHO` | Ausente. |
| `TIPO_OPERACION` | Ausente. `TipoDeComprobante="I"` significa ingreso en CFDI y no equivale al tipo de operación aduanal. |
| `CLAVE_PEDIMENTO` | Ausente. |

### Fechas del trámite

| Campo | Situación en el CFDI |
|---|---|
| `FECHA_ENTRADA` | Ausente. `Fecha` es la emisión de la factura. |
| `FECHA_PAGO` | Ausente. `FechaTimbrado` es la fecha de certificación fiscal y no una fecha de pago del pedimento. |

### Valores aduanales e incrementables

| Campo | Situación en el CFDI |
|---|---|
| `VALOR_DOLARES` | Ausente. El ejemplo está expresado en MXN y su `Total` no debe renombrarse como valor aduanal en dólares. |
| `VALOR_ADUANA` | Ausente. `Total` y `SubTotal` son importes fiscales del CFDI, no el valor en aduana. |
| `PRECIO_PAGADO_VALOR_COMERCIAL` | Ausente como concepto aduanal. El subtotal fiscal no es necesariamente equivalente. |
| `VALOR_SEGUROS` | Ausente. |
| `SEGUROS` | Ausente. |
| `FLETES` | Ausente. |
| `EMBALAJES` | Ausente. |
| `OTROS_INCREMENTABLES` | Ausente. |

### Liquidación y referencias del pedimento

| Campo | Situación en el CFDI |
|---|---|
| `CUADRO_LIQUIDACION` | Ausente. Los impuestos del CFDI corresponden a la factura y no al cuadro de liquidación aduanal. |
| `LIQUIDACION_NORMALIZADA` | No puede calcularse sin `CUADRO_LIQUIDACION` ni contribuciones aduanales. |
| `QR_PEDIMENTO` | Ausente. El Timbre Fiscal Digital no es el QR del pedimento. |
| `DEPOSITO_REFERENCIADO_QR_VALIDO` | No puede calcularse sin el QR del pedimento. |

### Participantes, facturas y mercancías

| Campo | Situación en el CFDI |
|---|---|
| `AGENTE_ADUANAL_NOMBRE` | Ausente. No debe suponerse que el emisor del CFDI es el agente aduanal. |
| `FACTURAS` | El XML representa una factura individual, pero no contiene la tabla de facturas vinculadas que aparece en el pedimento. Folio, fecha y emisor podrían servir para construir parcialmente una fila solo si negocio define explícitamente esa transformación. |
| `PAIS_ORIGEN` | Ausente. Los conceptos CFDI no declaran país de origen. |

## Campo con nombre coincidente pero semántica no garantizada

`TIPO_CAMBIO` existe en ambos catálogos, pero en este XML es el atributo
`cfdi:Comprobante/@TipoCambio`. Representa el tipo de cambio aplicable a la
moneda del CFDI. No debe asumirse automáticamente que coincide con el tipo de
cambio impreso en el encabezado del pedimento; esa igualdad tendría que
validarse contra el documento aduanal.

De igual forma:

- `CONCEPTOS` contiene partidas comerciales de la factura, no partidas del
  pedimento.
- `IMPUESTOS` contiene impuestos fiscales del CFDI, no contribuciones de la
  liquidación aduanal.
- `TIMBRE_FISCAL_DIGITAL.UUID` identifica el CFDI, no el pedimento.
- `EMISOR` y `RECEPTOR` no sustituyen automáticamente al agente aduanal,
  importador, exportador o proveedor declarados en un pedimento.

## Complementos ausentes

El único elemento dentro de `cfdi:Complemento` en el archivo revisado es
`tfd:TimbreFiscalDigital`. No existe un complemento de Comercio Exterior ni
otro bloque estructurado que aporte claves de pedimento, datos de mercancías
aduanales o domicilios de comercio exterior.

Aun si otro CFDI incluyera un complemento de Comercio Exterior, se tendría
que crear el mapeo de sus namespaces y `xpath` por separado y verificar qué
campos aporta realmente. Ese complemento tampoco debe darse por equivalente a
un pedimento completo sin comparar su contenido contra el catálogo PDF.

## Fuente necesaria para completar los campos

Para llenar los campos anteriores hace falta al menos una de estas fuentes:

1. El pedimento PDF procesado con `PEDIMENTO_ADUANAL`.
2. Un XML o export estructurado del sistema aduanal que contenga explícitamente
   identificación, partidas, incrementables, contribuciones y liquidación.
3. Una respuesta de API del sistema de comercio exterior con esos mismos
   datos y una relación verificable con el CFDI.

Cuando exista una fuente XML aduanal real, debe conservarse como fixture de
prueba y compararse ruta por ruta antes de ampliar
`PEDIMENTO_ADUANAL_XML.json`. Los campos solo deben marcarse `required` cuando
el nodo sea obligatorio y estable en todas las versiones que se pretenda
soportar.

## Consideraciones operativas

- La carga del archivo debe declarar explícitamente
  `documentType=PEDIMENTO_ADUANAL_XML`; no hay clasificación visual para XML
  crudo.
- La extracción XML es determinística y no usa Bedrock.
- Un valor ausente debe permanecer `null` o sin mapear; no debe copiarse desde
  un campo CFDI de significado distinto.
- No deben aplicarse al CFDI las fórmulas de valor en aduana, incrementables o
  liquidación mientras sus operandos aduanales no existan en la fuente.
- Antes de considerar completa una futura versión, debe repetirse la auditoría
  de cobertura sobre el nuevo XML y una comparación semántica contra
  `PEDIMENTO_ADUANAL.json`.
