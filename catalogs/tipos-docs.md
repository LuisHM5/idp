# Nombres de documentos y `TYPE_ID`

Solo los JSON fuera de cualquier carpeta `save/` están activos. Los nombres
históricos sin sufijo `_XML` que permanezcan en `catalogs/save/` son referencia
de negocio, no tipos disponibles para procesar de fábrica.

| Documento de negocio | Tipo activo | Nota |
|---|---|---|
| Pedimento | `PEDIMENTO_ADUANAL` | Pedimento PDF/imagen. |
| Factura CFDI que referencia un pedimento, con salida ERP | `PEDIMENTO_ERP_XML` | CFDI 4.0 con `InformacionAduanera/@NumeroPedimento`. Mapea número/componentes, factura y conceptos; no sustituye el pedimento completo. |
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

Todos los tipos `_XML` exigen `documentType` explícito en la subida y usan
extracción determinística por XPath, con costo Bedrock de extracción igual a
cero. Ver `catalogs/README.md`, sección “Tipos con fuente XML”.

`PEDIMENTO_ERP_XML` es deliberadamente distinto de `PEDIMENTO_ERP`: el segundo
lee el pedimento PDF completo y puede poblar clave, fechas, valores aduanales,
incrementables y liquidación; el primero lee una **factura** que solo cita el
pedimento y deja fuera todo dato que no esté presente en el CFDI.
