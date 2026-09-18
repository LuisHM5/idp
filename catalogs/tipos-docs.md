# Nombres de documentos y `TYPE_ID`

Solo los JSON fuera de cualquier carpeta `save/` están activos. Los nombres
históricos sin sufijo `_XML` que permanezcan en `catalogs/save/` son referencia
de negocio, no tipos disponibles para procesar de fábrica.

| Documento de negocio | Tipo activo | Nota |
|---|---|---|
| Pedimento | `PEDIMENTO_ADUANAL` | Pedimento PDF/imagen. |
| Factura CFDI que referencia un pedimento, con salida ERP | `PEDIMENTO_ERP_XML` | CFDI 4.0 con `InformacionAduanera/@NumeroPedimento`. Mapea número/componentes, factura y conceptos; no sustituye el pedimento completo. |
| Cualquier factura que se captura como un gasto del pedimento, con salida ERP | `PEDIMENTO_GASTOS_ADUANALES_ERP` o `PEDIMENTO_GASTOS_ADUANALES_ERP_XML` | Un renglón de la tabla `facturas_aduanales` del ERP: honorarios de la agencia, derecho de acceso, maniobras, muestreo, almacenaje, revisión/certificado fitosanitario, pago de derechos, logística. El primero lee la representación impresa (con recuperación del UUID y el total desde el QR del SAT); el segundo, el CFDI nativo sin Bedrock. Los dos entregan **los mismos campos con los mismos nombres**, así que el ERP inserta con un solo mapeo y elige la fuente según el archivo que le llegó. Fuera de la clasificación automática: se piden declarándolos. |
| Credencial para votar / INE | `INE` | Identificación mexicana. |
| Certificado, carátula o constancia de seguro | `CERTIFICADO_POLIZA` | Documento de póliza o cobertura. |
| Factura de honorarios / honorarios de agencia aduanal | `FACTURA_HONORARIOS` o `FACTURA_HONORARIOS_XML` | El primero procesa PDF/impreso; el segundo procesa el CFDI XML nativo sin Bedrock. |
| Honorarios extranjeros | No hay un tipo activo específico | No usar `FACTURA_HONORARIOS_XML`: HE corresponde a un proveedor extranjero y normalmente no es CFDI mexicano. |
| Operaciones logísticas | `FACTURA_LOGISTICA_XML` | CFDI XML por coordinación o servicios logísticos. No equivale automáticamente a flete. |
| Gastos de terceros | `CFDI_GASTO_TERCERO_XML` | CFDI XML por acceso, maniobras, muestreo, almacenaje, inspección u otros gastos operativos del despacho. |
| Pago de derechos sanitarios | `CFDI_PAGO_DERECHOS_SANITARIOS_XML` | CFDI XML que acredita el cobro de un derecho o servicio de SENASICA, COFEPRIS u otra autoridad. No es el permiso/certificado emitido. |
| Factura de flete extranjero / Freight Invoice / Pago Flete Americano | `FACTURA_FLETE_EXTRANJERO` | Factura extranjera no-CFDI. El tipo cubre mercancía, transporte y servicios extranjeros; conserva el nombre por compatibilidad. |
| PDF completo con varios anexos | No hay un tipo activo específico | El antiguo `EXPEDIENTE_ADUANAL_MULTIDOCUMENTO` está estacionado en `save/`; el pipeline puede procesar varios tipos declarados sin reactivarlo. |

## Correspondencia de los XML de gastos aduanales

| Identificador usado en el expediente | `TYPE_ID` que se debe declarar al subir | A qué se refiere |
|---|---|---|
| `FAA` | `FACTURA_HONORARIOS_XML` | Honorarios nacionales de agencia o servicio aduanal emitidos mediante CFDI mexicano. En la muestra, una persona física cobra el concepto `HONORARIOS`; las retenciones de ISR/IVA no lo convierten en honorario extranjero. |
| `GT1` | `CFDI_PAGO_DERECHOS_SANITARIOS_XML` | Pago por un certificado/permiso sanitario o fitosanitario. En la muestra, SENASICA cobra el derecho por un certificado fitosanitario de importación; el XML es el comprobante fiscal del pago, no el certificado. |
| `GT2` | `CFDI_GASTO_TERCERO_XML` | Gasto de un proveedor tercero necesario para el despacho. En la muestra: acceso de vehículo, maniobras y muestreo de carga. |
| `OLI` | `FACTURA_LOGISTICA_XML` | Operación o servicio logístico ligado al comercio exterior. Solo debe tratarse como flete cuando el concepto declare transporte/flete de forma explícita. |

**El sufijo del archivo no es el tipo.** Esa correspondencia describe UNA muestra,
no una convención estable: en `test-docs/otros-gastos` (pedimento 26 07 0823
6001803) el `GT1` es un laboratorio de agrodiagnóstico y el `GT2` es el cobro de
SENASICA, o sea justo al revés que en la tabla de arriba, y el `FPE` no es un
gasto aduanal sino la factura de la mercancía del proveedor extranjero (el tipo
para ése es `FACTURA_FLETE_EXTRANJERO`). Clasifica por lo que dice el
comprobante — emisor y concepto —, nunca por cómo se nombró el archivo. Cuando
lo único que se necesita es el renglón de gastos del ERP y no la lectura fiscal
por familia, la pareja `PEDIMENTO_GASTOS_ADUANALES_ERP`/`_XML` cubre las cuatro
familias (`FAA`, `GT*`, `OLI`) con un solo contrato.

### `SIGLAS_DOCUMENTO` y `TIPO_DOCUMENTO` en la pareja de gastos ERP

Como esa pareja abarca varios documentos del expediente, cada resultado dice
cuál es con dos campos: la sigla **canónica** y el nombre que le corresponde.

| En el nombre del archivo | `SIGLAS_DOCUMENTO` | `TIPO_DOCUMENTO` | PDF | XML |
|---|---|---|---|---|
| `P` | `P` | Pedimento | Sí | No |
| `HE` | `HE` | Honorarios Extranjeros | Sí | No |
| `II`, `FAA` | `II` | Honorarios | Sí | Sí |
| `OLI` | `OLI` | Operaciones Logísticas | Sí | Sí |
| `PFA` | `PFA` | Fletes | Sí | No |
| `GT`, `GT1`, `GT2`, ... | `GT` | Otros Servicios Aduanales / Gastos de Terceros | Sí | Sí |
| `PS` | `PS` | Permisos de Salud | Sí | No |
| `FPE`, `PE` | `FPE` | Factura de Proveedores Extranjeros | Sí | No |

Se decide **primero por el nombre del archivo** (`<pedimento>_<SIGLA><n>.<ext>`,
la sigla al final y antes de la extensión; determinista, $0, confianza 1.0) y,
si el nombre no la trae, **por contenido**: en el PDF la lee el modelo; en el XML
son reglas sobre el CFDI (retención de ISR → `II`, primer concepto de logística
→ `OLI`, cualquier otro CFDI timbrado → `GT`). El consecutivo (`GT3`) y la
variante (`FAA`) no se pierden: siguen en el nombre del archivo.

Esto **no contradice** la advertencia de arriba. La sigla dice la FAMILIA del
documento (`GT` = gasto de tercero), y ésa sí es una convención estable de quien
sube; lo que no es estable es qué gasto concreto trae el `GT1` contra el `GT2`,
y por eso el `TYPE_ID` fino (`CFDI_PAGO_DERECHOS_SANITARIOS_XML` contra
`CFDI_GASTO_TERCERO_XML`) se sigue eligiendo por contenido. La tabla completa se
mapea aunque `P` y `FPE` no sean gastos: si alguien sube uno de ésos con este
tipo, el resultado lo dice en vez de etiquetarlo como gasto.

Todos los tipos `_XML` exigen `documentType` explícito en la subida y usan
extracción determinística por XPath, con costo Bedrock de extracción igual a
cero. Ver `catalogs/README.md`, sección “Tipos con fuente XML”.

`PEDIMENTO_ERP_XML` es deliberadamente distinto de `PEDIMENTO_ERP`: el segundo
lee el pedimento PDF completo y puede poblar clave, fechas, valores aduanales,
incrementables y liquidación; el primero lee una **factura** que solo cita el
pedimento y deja fuera todo dato que no esté presente en el CFDI.
