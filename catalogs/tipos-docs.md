Pedimento PEDIMENTO_ADUANAL
Credencial para Votar / INE INE
Certificado, carátula o constancia de seguro CERTIFICADO_POLIZA
Factura de Honorarios / Honorarios de Agencia Aduanal FACTURA_HONORARIOS (PDF/impreso) o FACTURA_HONORARIOS_XML (XML CFDI nativo, mapeo determinístico sin Bedrock — ver catalogs/README.md "Tipos con fuente XML")
Factura Agencia Aduanal FACTURA_AGENCIA_ADUANAL o CUENTA_GASTOS_AGENTE_ADUANAL
Honorarios Extranjeros FACTURA_PROVEEDOR_EXTRANJERO, con subtipo de honorarios, o crear FACTURA_HONORARIOS_EXTRANJEROS
Operaciones Logísticas Internacionales FACTURA_LOGISTICA o DOCUMENTO_TRANSPORTE
Factura Proveedores Extranjeros FACTURA_PROVEEDOR_EXTRANJERO — no existe como tipo propio; hoy lo cubre FACTURA_FLETE_EXTRANJERO (ver abajo)
Gastos Terceros CFDI_GASTO_TERCERO
Factura de Flete Extranjero / Freight Invoice / Pago Flete Americano FACTURA_FLETE_EXTRANJERO — el tipo cubre TODA factura extranjera no-CFDI, no solo flete: mercancía, transporte y servicios (almacenaje, maniobras). Declara `multi_instance`, así que un PDF con varias facturas produce un documento por factura y las copias repetidas colapsan (ver catalogs/README.md). El TYPE_ID conserva el nombre por compatibilidad con `raw/{TYPE}/` y los clientes de la API
Permiso Salud PERMISO_CERTIFICADO_SANITARIO
PDF completo con varios anexos EXPEDIENTE_ADUANAL_MULTIDOCUMENTO
