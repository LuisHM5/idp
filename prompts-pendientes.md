- mejora la seguridad del sistema
- necesito que mejores esta app con las siguientes consideraciones
  quiero tener un modo dev y prod
  quiero que en prod tenga auth por token y todas las rutas protegidas
- mejorar
  - buckets privados solo accesibles por app o gestion
  - endpoint protegicos
  - y ratelimits configurables general y por documento o endpoint
  - anti ddos y ataques de fuerza bruta
  - consulta segura y auditoria de quien y que paso en todo momento
  - api keys para otros sistemas
    y lo pendiente no implementado de seguridad

Necesito diseñar e implementar una plataforma middleware para la extracción y validación automática de documentos. La solución será utilizada por diferentes aplicaciones internas, cada una con sus propios tipos de documentos, campos, reglas de extracción y reglas de validación.

El sistema será completamente backend. No se requiere interfaz gráfica en la primera versión, pero todas las APIs, procesos, configuraciones y flujos de integración deberán quedar claramente documentados.

## 1. Objetivo general

Diseñar una plataforma serverless, segura, escalable, configurable y eficiente en costos que permita:

1. Registrar aplicaciones consumidoras.
2. Autenticar cada aplicación mediante API key, token OpenID Connect u otro mecanismo seguro entre sistemas.
3. Registrar diferentes tipos de documentos.
4. Configurar campos que deben extraerse de cada documento.
5. Configurar reglas dinámicas de extracción y validación.
6. Recibir documentos mediante una API.
7. Extraer información utilizando OCR e inteligencia artificial.
8. Validar cada campo con reglas configurables.
9. Calcular porcentajes de confianza.
10. Entregar una resolución final por documento y por campo.
11. Evitar entregar información no confiable como si fuera correcta.
12. Permitir entrenamiento o mejora progresiva mediante documentos de ejemplo.

## 2. Restricciones obligatorias

La solución deberá ser totalmente serverless y utilizar servicios administrados por AWS.

Considerar principalmente:

- AWS Lambda.
- Amazon API Gateway.
- Amazon DynamoDB.
- Amazon SQS.
- AWS Step Functions cuando sea conveniente.
- Amazon EventBridge cuando aporte valor.
- Amazon Textract para OCR y extracción documental.
- Amazon Bedrock u otros servicios administrados de inteligencia artificial.
- Amazon Cognito o integración con un proveedor OpenID Connect.
- AWS Secrets Manager.
- AWS KMS.
- AWS WAF.
- AWS Shield.
- Amazon CloudWatch.
- AWS CloudTrail.
- AWS X-Ray.
- Amazon S3 únicamente cuando sea necesario para el procesamiento temporal y, en una fase futura, para conservar respaldos documentales.

No utilizar servidores tradicionales, máquinas virtuales ni bases de datos administradas manualmente, salvo que exista una justificación técnica indispensable.

El despliegue deberá estar automatizado mediante infraestructura como código y servicios administrados por AWS. Evaluar AWS CDK, AWS SAM o Terraform y recomendar una opción.

## 3. Arquitectura multiaplicación

El sistema debe soportar múltiples aplicaciones consumidoras.

Cada aplicación deberá tener:

- Identificador único.
- Nombre.
- Propietario o área responsable.
- API keys o credenciales asociadas.
- Estado activo o inactivo.
- Límites de consumo.
- Direcciones IP permitidas, opcionalmente.
- Tipos de documentos autorizados.
- Reglas particulares de extracción.
- Reglas particulares de validación.
- Historial de llamadas.
- Métricas de uso.
- Configuración de webhooks, si aplica.

El sistema debe identificar qué aplicación está realizando cada solicitud y aplicar automáticamente la configuración correspondiente.

Una aplicación solamente podrá procesar los tipos de documentos para los que tenga autorización.

## 4. Tipos de documentos

Debe existir un catálogo general de tipos de documentos.

Un tipo de documento podrá ser:

- Global: disponible para varias aplicaciones.
- Específico: disponible solamente para una aplicación.
- Heredado: una aplicación utiliza la configuración global y sobrescribe ciertos campos o reglas.
- Versionado: diferentes versiones del mismo documento pueden tener estructuras, campos o reglas distintas.

Ejemplos:

- Factura.
- Identificación oficial.
- Póliza.
- Certificado.
- Contrato.
- Estado de cuenta.
- Comprobante de domicilio.
- Documento específico de una aplicación.

La relación principal deberá considerar:

`Aplicación + Tipo de documento + Versión de configuración`

Una aplicación puede procesar un mismo tipo de documento de manera distinta a otra aplicación.

## 5. Registro dinámico de documentos

La plataforma deberá permitir registrar un nuevo tipo de documento mediante una definición JSON, sin modificar el código principal.

Diseña un esquema JSON versionado que permita configurar como mínimo:

- Identificador del tipo de documento.
- Nombre.
- Descripción.
- Aplicación propietaria o alcance global.
- Versión.
- Formatos permitidos.
- Tamaño máximo.
- Número máximo de páginas.
- Campos que deben extraerse.
- Tipo de dato esperado por campo.
- Campo obligatorio u opcional.
- Expresiones regulares.
- Longitud mínima y máxima.
- Valores permitidos.
- Catálogos de referencia.
- Reglas cruzadas entre campos.
- Posición aproximada del campo.
- Página esperada.
- Etiquetas cercanas.
- Palabras clave.
- Coordenadas o zonas del documento.
- Estrategia de extracción.
- Nivel mínimo de confianza.
- Reglas de normalización.
- Reglas de validación.
- Dependencias entre campos.
- Resolución esperada.
- Política de baja confianza.
- Estrategia de revisión.
- Configuración de entrenamiento o ejemplos.
- Estado activo o inactivo.
- Fecha de vigencia.
- Historial de versiones.

Incluye un ejemplo completo del JSON para registrar un documento.

## 6. Extracción de información

La solución deberá extraer campos específicos según la configuración registrada.

Debe soportar diferentes estrategias:

1. Extracción mediante OCR.
2. Extracción por formularios y tablas.
3. Extracción mediante consultas configurables.
4. Extracción mediante etiquetas o palabras cercanas.
5. Extracción mediante coordenadas o zonas.
6. Extracción basada en posiciones aproximadas.
7. Extracción con modelos previamente entrenados.
8. Extracción con modelos generativos como mecanismo complementario.
9. Extracción híbrida combinando reglas, OCR e inteligencia artificial.

Cada campo podrá definir su estrategia de extracción.

Ejemplo:

- RFC localizado después de la etiqueta “RFC”.
- Total ubicado en la parte inferior derecha.
- Número de póliza ubicado dentro de una zona específica.
- Nombre obtenido mediante una consulta semántica.
- Fecha normalizada al formato ISO 8601.
- Tabla de productos extraída como una colección.

La solución deberá admitir documentos con diferentes formatos, ubicaciones y variaciones visuales.

## 7. Documentos de entrenamiento y ejemplos

Debe ser posible proporcionar documentos de ejemplo para mejorar la precisión.

Considerar:

- Documentos positivos.
- Documentos negativos.
- Diferentes diseños del mismo documento.
- Versiones antiguas y nuevas.
- Documentos con baja calidad.
- Documentos escaneados.
- Documentos fotografiados.
- Documentos con campos marcados.
- Coordenadas de campos.
- Cuadros o regiones resaltadas.
- Etiquetas esperadas.
- Valores reales esperados.
- Correcciones realizadas por una persona.

Diseña un proceso para:

1. Registrar los documentos de entrenamiento.
2. Etiquetar campos y coordenadas.
3. Separar entrenamiento, validación y pruebas.
4. Entrenar o adaptar el modelo.
5. Evaluar su precisión.
6. Publicar una nueva versión.
7. Comparar la versión actual contra la anterior.
8. Realizar rollback si disminuye la precisión.
9. Mantener trazabilidad de los datos utilizados.

No asumas que todos los servicios de AWS permiten entrenamiento personalizado de la misma manera. Explica qué servicios admiten entrenamiento, adaptadores, consultas, prompts, ejemplos o ajuste del modelo.

## 8. Validaciones dinámicas

Los campos extraídos deberán validarse mediante reglas configuradas dinámicamente.

Tipos de validación requeridos:

- Tipo de dato.
- Campo obligatorio.
- Formato.
- Expresión regular.
- Longitud.
- Rango numérico.
- Rango de fechas.
- Valor permitido.
- Comparación contra catálogos.
- Comparación entre campos.
- Operaciones matemáticas.
- Validación de totales.
- Consistencia entre fechas.
- Consistencia entre nombre, identificador y otros campos.
- Validación mediante servicio externo.
- Validación mediante función Lambda personalizada.
- Validación mediante inteligencia artificial.
- Validaciones específicas por aplicación.
- Reglas condicionales.
- Reglas dependientes de la versión del documento.

Ejemplos:

- La fecha final debe ser posterior a la fecha inicial.
- El subtotal más impuestos debe coincidir con el total.
- El RFC debe cumplir con su estructura esperada.
- El número de póliza debe existir en un catálogo.
- La suma de los importes debe coincidir con el total declarado.
- Si el tipo de persona es moral, el RFC debe tener la longitud correspondiente.
- Si un campo aparece vacío, otra regla puede determinar si es obligatorio.

Diseña un motor de reglas dinámicas que no requiera desplegar nuevamente la aplicación al agregar o modificar una regla.

## 9. Confianza y resolución

La respuesta deberá incluir la confianza de cada campo y la resolución general del documento.

Estados mínimos por campo:

- `VALIDATED`
- `INVALID`
- `LOW_CONFIDENCE`
- `NOT_FOUND`
- `NOT_APPLICABLE`
- `REVIEW_REQUIRED`
- `ERROR`

Estados mínimos del documento:

- `APPROVED`
- `REJECTED`
- `PARTIALLY_APPROVED`
- `REVIEW_REQUIRED`
- `PROCESSING_ERROR`

Cada campo deberá devolver:

- Valor original extraído.
- Valor normalizado.
- Tipo de dato.
- Fuente de extracción.
- Página.
- Coordenadas, cuando estén disponibles.
- Porcentaje de confianza.
- Reglas ejecutadas.
- Resultado de cada regla.
- Errores encontradola app ya esta medio implementada pero la satisfacción de extracción es muy mala y no considere todo lo siguiente mejorala para que sea realista y al menos solucione al 100% el caso de pedimentos
  Actúa como un arquitecto de soluciones AWS senior, especialista en sistemas serverless, procesamiento inteligente de documentos, seguridad de APIs, machine learning, OCR y diseño de plataformas multiaplicación.

Necesito diseñar e implementar una plataforma middleware para la extracción y validación automática de documentos. La solución será utilizada por diferentes aplicaciones internas, cada una con sus propios tipos de documentos, campos, reglas de extracción y reglas de validación.

El sistema será completamente backend. No se requiere interfaz gráfica en la primera versión, pero todas las APIs, procesos, configuraciones y flujos de integración deberán quedar claramente documentados.

## 1. Objetivo general

Diseñar una plataforma serverless, segura, escalable, configurable y eficiente en costos que permita:

1. Registrar aplicaciones consumidoras.
2. Autenticar cada aplicación mediante API key, token OpenID Connect u otro mecanismo seguro entre sistemas.
3. Registrar diferentes tipos de documentos.
4. Configurar campos que deben extraerse de cada documento.
5. Configurar reglas dinámicas de extracción y validación.
6. Recibir documentos mediante una API.
7. Extraer información utilizando OCR e inteligencia artificial.
8. Validar cada campo con reglas configurables.
9. Calcular porcentajes de confianza.
10. Entregar una resolución final por documento y por campo.
11. Evitar entregar información no confiable como si fuera correcta.
12. Permitir entrenamiento o mejora progresiva mediante documentos de ejemplo.

## 2. Restricciones obligatorias

La solución deberá ser totalmente serverless y utilizar servicios administrados por AWS.

Considerar principalmente:

- AWS Lambda.
- Amazon API Gateway.
- Amazon DynamoDB.
- Amazon SQS.
- AWS Step Functions cuando sea conveniente.
- Amazon EventBridge cuando aporte valor.
- Amazon Textract para OCR y extracción documental.
- Amazon Bedrock u otros servicios administrados de inteligencia artificial.
- Amazon Cognito o integración con un proveedor OpenID Connect.
- AWS Secrets Manager.
- AWS KMS.
- AWS WAF.
- AWS Shield.
- Amazon CloudWatch.
- AWS CloudTrail.
- AWS X-Ray.
- Amazon S3 únicamente cuando sea necesario para el procesamiento temporal y, en una fase futura, para conservar respaldos documentales.

No utilizar servidores tradicionales, máquinas virtuales ni bases de datos administradas manualmente, salvo que exista una justificación técnica indispensable.

El despliegue deberá estar automatizado mediante infraestructura como código y servicios administrados por AWS. Evaluar AWS CDK, AWS SAM o Terraform y recomendar una opción.

## 3. Arquitectura multiaplicación

El sistema debe soportar múltiples aplicaciones consumidoras.

Cada aplicación deberá tener:

- Identificador único.
- Nombre.
- Propietario o área responsable.
- API keys o credenciales asociadas.
- Estado activo o inactivo.
- Límites de consumo.
- Direcciones IP permitidas, opcionalmente.
- Tipos de documentos autorizados.
- Reglas particulares de extracción.
- Reglas particulares de validación.
- Historial de llamadas.
- Métricas de uso.
- Configuración de webhooks, si aplica.

El sistema debe identificar qué aplicación está realizando cada solicitud y aplicar automáticamente la configuración correspondiente.

Una aplicación solamente podrá procesar los tipos de documentos para los que tenga autorización.

## 4. Tipos de documentos

Debe existir un catálogo general de tipos de documentos.

Un tipo de documento podrá ser:

- Global: disponible para varias aplicaciones.
- Específico: disponible solamente para una aplicación.
- Heredado: una aplicación utiliza la configuración global y sobrescribe ciertos campos o reglas.
- Versionado: diferentes versiones del mismo documento pueden tener estructuras, campos o reglas distintas.

Ejemplos:

- Factura.
- Identificación oficial.
- Póliza.
- Certificado.
- Contrato.
- Estado de cuenta.
- Comprobante de domicilio.
- Documento específico de una aplicación.

La relación principal deberá considerar:

`Aplicación + Tipo de documento + Versión de configuración`

Una aplicación puede procesar un mismo tipo de documento de manera distinta a otra aplicación.

## 5. Registro dinámico de documentos

La plataforma deberá permitir registrar un nuevo tipo de documento mediante una definición JSON, sin modificar el código principal.

Diseña un esquema JSON versionado que permita configurar como mínimo:

- Identificador del tipo de documento.
- Nombre.
- Descripción.
- Aplicación propietaria o alcance global.
- Versión.
- Formatos permitidos.
- Tamaño máximo.
- Número máximo de páginas.
- Campos que deben extraerse.
- Tipo de dato esperado por campo.
- Campo obligatorio u opcional.
- Expresiones regulares.
- Longitud mínima y máxima.
- Valores permitidos.
- Catálogos de referencia.
- Reglas cruzadas entre campos.
- Posición aproximada del campo.
- Página esperada.
- Etiquetas cercanas.
- Palabras clave.
- Coordenadas o zonas del documento.
- Estrategia de extracción.
- Nivel mínimo de confianza.
- Reglas de normalización.
- Reglas de validación.
- Dependencias entre campos.
- Resolución esperada.
- Política de baja confianza.
- Estrategia de revisión.
- Configuración de entrenamiento o ejemplos.
- Estado activo o inactivo.
- Fecha de vigencia.
- Historial de versiones.

Incluye un ejemplo completo del JSON para registrar un documento.

## 6. Extracción de información

La solución deberá extraer campos específicos según la configuración registrada.

Debe soportar diferentes estrategias:

1. Extracción mediante OCR.
2. Extracción por formularios y tablas.
3. Extracción mediante consultas configurables.
4. Extracción mediante etiquetas o palabras cercanas.
5. Extracción mediante coordenadas o zonas.
6. Extracción basada en posiciones aproximadas.
7. Extracción con modelos previamente entrenados.
8. Extracción con modelos generativos como mecanismo complementario.
9. Extracción híbrida combinando reglas, OCR e inteligencia artificial.

Cada campo podrá definir su estrategia de extracción.

Ejemplo:

- RFC localizado después de la etiqueta “RFC”.
- Total ubicado en la parte inferior derecha.
- Número de póliza ubicado dentro de una zona específica.
- Nombre obtenido mediante una consulta semántica.
- Fecha normalizada al formato ISO 8601.
- Tabla de productos extraída como una colección.

La solución deberá admitir documentos con diferentes formatos, ubicaciones y variaciones visuales.

## 7. Documentos de entrenamiento y ejemplos

Debe ser posible proporcionar documentos de ejemplo para mejorar la precisión.

Considerar:

- Documentos positivos.
- Documentos negativos.
- Diferentes diseños del mismo documento.
- Versiones antiguas y nuevas.
- Documentos con baja calidad.
- Documentos escaneados.
- Documentos fotografiados.
- Documentos con campos marcados.
- Coordenadas de campos.
- Cuadros o regiones resaltadas.
- Etiquetas esperadas.
- Valores reales esperados.
- Correcciones realizadas por una persona.

Diseña un proceso para:

1. Registrar los documentos de entrenamiento.
2. Etiquetar campos y coordenadas.
3. Separar entrenamiento, validación y pruebas.
4. Entrenar o adaptar el modelo.
5. Evaluar su precisión.
6. Publicar una nueva versión.
7. Comparar la versión actual contra la anterior.
8. Realizar rollback si disminuye la precisión.
9. Mantener trazabilidad de los datos utilizados.

No asumas que todos los servicios de AWS permiten entrenamiento personalizado de la misma manera. Explica qué servicios admiten entrenamiento, adaptadores, consultas, prompts, ejemplos o ajuste del modelo.

## 8. Validaciones dinámicas

Los campos extraídos deberán validarse mediante reglas configuradas dinámicamente.

Tipos de validación requeridos:

- Tipo de dato.
- Campo obligatorio.
- Formato.
- Expresión regular.
- Longitud.
- Rango numérico.
- Rango de fechas.
- Valor permitido.
- Comparación contra catálogos.
- Comparación entre campos.
- Operaciones matemáticas.
- Validación de totales.
- Consistencia entre fechas.
- Consistencia entre nombre, identificador y otros campos.
- Validación mediante servicio externo.
- Validación mediante función Lambda personalizada.
- Validación mediante inteligencia artificial.
- Validaciones específicas por aplicación.
- Reglas condicionales.
- Reglas dependientes de la versión del documento.

Ejemplos:

- La fecha final debe ser posterior a la fecha inicial.
- El subtotal más impuestos debe coincidir con el total.
- El RFC debe cumplir con su estructura esperada.
- El número de póliza debe existir en un catálogo.
- La suma de los importes debe coincidir con el total declarado.
- Si el tipo de persona es moral, el RFC debe tener la longitud correspondiente.
- Si un campo aparece vacío, otra regla puede determinar si es obligatorio.

Diseña un motor de reglas dinámicas que no requiera desplegar nuevamente la aplicación al agregar o modificar una regla.

## 9. Confianza y resolución

La respuesta deberá incluir la confianza de cada campo y la resolución general del documento.

Estados mínimos por campo:

- `VALIDATED`
- `INVALID`
- `LOW_CONFIDENCE`
- `NOT_FOUND`
- `NOT_APPLICABLE`
- `REVIEW_REQUIRED`
- `ERROR`

Estados mínimos del documento:

- `APPROVED`
- `REJECTED`
- `PARTIALLY_APPROVED`
- `REVIEW_REQUIRED`
- `PROCESSING_ERROR`

Cada campo deberá devolver:

- Valor original extraído.
- Valor normalizado.
- Tipo de dato.
- Fuente de extracción.
- Página.
- Coordenadas, cuando estén disponibles.
- Porcentaje de confianza.
- Reglas ejecutadas.
- Resultado de cada regla.
- Errores encontrados.
- Advertencias.
- Resolución final.
- Evidencia o explicación breve.
- Versión de configuración utilizada.
- Versión del modelo utilizado.

El documento deberá devolver:

- Identificador de procesamiento.
- Aplicación solicitante.
- Tipo de documento detectado o declarado.
- Resolución general.
- Confianza general.
- Campos extraídos.
- Campos validados.
- Errores.
- Advertencias.
- Reglas ejecutadas.
- Tiempo de procesamiento.
- Costo aproximado del procesamiento, cuando sea viable.
- Identificador de trazabilidad.
- Fecha de procesamiento.

## 10. Política contra datos inventados

La plataforma no deberá entregar valores inciertos como si fueran correctos.

Define una política estricta:

1. Si el campo tiene confianza suficiente y supera sus validaciones, devolverlo como validado.
2. Si la confianza es baja, marcarlo como `LOW_CONFIDENCE` o `REVIEW_REQUIRED`.
3. Si el valor no puede determinarse, devolver `NOT_FOUND`.
4. No inventar valores faltantes.
5. No sustituir automáticamente el valor original sin conservar evidencia.
6. Cualquier corrección sugerida por inteligencia artificial deberá devolverse por separado como sugerencia.
7. La respuesta deberá distinguir claramente entre:
   - Valor extraído.
   - Valor normalizado.
   - Valor sugerido.
   - Valor validado.

8. Una sugerencia generada por IA nunca deberá considerarse validada únicamente por haber sido generada.
9. La aplicación consumidora podrá decidir si acepta la sugerencia, solicita revisión o rechaza el documento.
10. Toda inferencia deberá incluir nivel de confianza y explicación.

En casos de baja confianza, se puede utilizar inteligencia artificial para proponer una posible corrección, pero nunca deberá ocultarse que se trata de una inferencia.

Ejemplo:

```json
{
  "extractedValue": "ABCI23",
  "normalizedValue": null,
  "suggestedValue": "ABC123",
  "confidence": 0.61,
  "status": "REVIEW_REQUIRED",
  "suggestionSource": "GENERATIVE_AI",
  "automaticallyAccepted": false
}
```

## 11. Procesamiento síncrono y asíncrono

Debido a que el procesamiento de documentos puede superar el tiempo máximo recomendable de una solicitud HTTP, diseña dos modalidades:

### Procesamiento síncrono

Para documentos pequeños y procesos rápidos:

1. La aplicación envía el documento.
2. El sistema procesa el documento.
3. La misma solicitud devuelve la información extraída y validada.

### Procesamiento asíncrono

Para documentos grandes o procesamiento prolongado:

1. La aplicación envía el documento.
2. El sistema devuelve un identificador de procesamiento.
3. El documento se coloca en una cola.
4. Lambdas o Step Functions realizan el procesamiento.
5. La aplicación consulta el estado mediante una API.
6. Opcionalmente, el sistema notifica el resultado mediante webhook.

Propón criterios configurables para elegir entre procesamiento síncrono y asíncrono.

No mantengas conexiones HTTP abiertas innecesariamente.

## 12. APIs requeridas

Diseña una API REST versionada. Incluye endpoints, contratos, códigos HTTP, validaciones y ejemplos de request y response.

Como mínimo, documenta APIs para:

### Aplicaciones

- Registrar una aplicación.
- Consultar una aplicación.
- Actualizar una aplicación.
- Desactivar una aplicación.
- Regenerar API key.
- Revocar API key.
- Consultar consumo.
- Configurar rate limit.
- Configurar IPs permitidas.
- Configurar webhook.

### Tipos de documentos

- Registrar un tipo de documento.
- Consultar tipos de documentos globales.
- Consultar tipos autorizados para una aplicación.
- Actualizar una configuración.
- Crear una nueva versión.
- Activar una versión.
- Desactivar una versión.
- Configurar campos.
- Configurar reglas.
- Configurar zonas y coordenadas.
- Asociar un documento a una aplicación.

### Procesamiento

- Enviar documento directamente.
- Solicitar una URL prefirmada de carga cuando sea necesario.
- Iniciar procesamiento.
- Consultar estado.
- Consultar resultado.
- Reintentar procesamiento.
- Cancelar procesamiento.
- Descargar evidencia, cuando esté permitida.
- Enviar correcciones.
- Registrar revisión manual.

### Entrenamiento

- Registrar conjunto de documentos.
- Cargar documentos de ejemplo.
- Etiquetar campos.
- Registrar coordenadas.
- Iniciar entrenamiento.
- Consultar estado del entrenamiento.
- Consultar métricas.
- Publicar versión.
- Realizar rollback.

Incluye ejemplos con `curl` y JSON.

## 13. Envío de documentos

Explica y compara las siguientes estrategias:

- `multipart/form-data`.
- Documento codificado en Base64.
- Carga directa a S3 mediante URL prefirmada.
- Referencia a un documento existente.
- Procesamiento por lotes.

Recomienda la estrategia principal considerando:

- Límites de API Gateway y Lambda.
- Tamaño de documentos.
- Seguridad.
- Costo.
- Rendimiento.
- Trazabilidad.
- Facilidad de integración.

No guardar documentos permanentemente en la primera versión, salvo que sea necesario. Define políticas de eliminación automática para archivos temporales.

## 14. Seguridad

La solución deberá incluir una estrategia de seguridad completa.

Considerar:

- OpenID Connect.
- OAuth 2.0 Client Credentials.
- API keys.
- JWT.
- Rotación de credenciales.
- Revocación de credenciales.
- Autorización por aplicación.
- Autorización por tipo de documento.
- Rutas protegidas.
- Cifrado en tránsito.
- Cifrado en reposo.
- AWS KMS.
- AWS Secrets Manager.
- Principio de menor privilegio.
- Roles IAM separados.
- Protección contra DDoS.
- AWS WAF.
- AWS Shield.
- Rate limiting por aplicación.
- Rate limiting por API key.
- Rate limiting por dirección IP.
- Cuotas diarias y mensuales.
- Bloqueo temporal por abuso.
- Validación de archivos.
- Validación de extensión y MIME type.
- Protección contra archivos maliciosos.
- Análisis antivirus o antimalware.
- Prevención de inyección.
- Prevención de NoSQL injection.
- Validación y sanitización de parámetros.
- Límites de tamaño.
- Límites de páginas.
- Protección contra descompresión maliciosa.
- Protección contra cargas repetidas.
- Idempotencia.
- Auditoría.
- Trazabilidad.
- Protección de información sensible.
- Eliminación segura.
- Políticas de retención.
- Parches y vulnerabilidades conocidas en dependencias de Lambda.

Aunque DynamoDB no utiliza SQL tradicional, incluye controles contra inyección, manipulación de expresiones, acceso indebido y entradas maliciosas.

## 15. Rate limiting y control de consumo

Cada aplicación deberá poder tener:

- Solicitudes por segundo.
- Solicitudes por minuto.
- Solicitudes por día.
- Cantidad máxima de documentos.
- Cantidad máxima de páginas.
- Tamaño máximo acumulado.
- Presupuesto máximo mensual.
- Concurrencia máxima.
- Modelos autorizados.
- Tipos de documento autorizados.
- Horarios permitidos.
- IPs permitidas.

El sistema deberá conocer qué aplicación está llamando y aplicar su límite correspondiente.

Explica cómo implementar los límites utilizando API Gateway, AWS WAF, DynamoDB, Lambda, usage plans u otros servicios.

## 16. Datos y modelo de DynamoDB

Diseña el modelo de datos de DynamoDB.

Considera entidades como:

- Aplicaciones.
- Credenciales.
- Tipos de documentos.
- Versiones de documentos.
- Configuraciones.
- Campos.
- Reglas.
- Relación entre aplicaciones y documentos.
- Solicitudes de procesamiento.
- Resultados.
- Auditoría.
- Consumo.
- Rate limits.
- Modelos.
- Entrenamientos.
- Correcciones.
- Webhooks.
- Idempotencia.

Propón:

- Partition keys.
- Sort keys.
- Índices secundarios.
- Patrones de acceso.
- TTL.
- Versionado.
- Bloqueo optimista.
- Estrategia de auditoría.
- Prevención de hot partitions.
- Separación entre configuración y resultados.

No diseñes las tablas como si DynamoDB fuera una base de datos relacional.

## 17. Observabilidad

La solución deberá registrar:

- Aplicación solicitante.
- Tipo de documento.
- Versión.
- Endpoint.
- Estado.
- Tiempo total.
- Tiempo por etapa.
- Número de páginas.
- Modelo utilizado.
- Consumo de tokens, cuando aplique.
- Costo estimado.
- Confianza promedio.
- Número de campos validados.
- Número de campos rechazados.
- Errores.
- Reintentos.
- Mensajes enviados a DLQ.
- Solicitudes bloqueadas.
- Rate limits alcanzados.

Considerar:

- CloudWatch Logs.
- CloudWatch Metrics.
- Alarmas.
- Dashboards.
- X-Ray.
- CloudTrail.
- Correlation ID.
- Trazas distribuidas.
- Dead-letter queues.
- Alertas por errores o incremento de costos.

No registrar documentos completos ni información sensible innecesariamente en los logs.

## 18. Manejo de errores

Diseña una estrategia para:

- Reintentos.
- Backoff exponencial.
- Dead-letter queues.
- Errores parciales.
- Timeouts.
- Fallos de modelos.
- Fallos de OCR.
- Fallos de reglas externas.
- Documentos corruptos.
- Formatos no admitidos.
- Documentos protegidos por contraseña.
- Documentos ilegibles.
- Procesamiento duplicado.
- Webhooks fallidos.
- Recuperación de procesos detenidos.
- Idempotencia.

Cada error deberá tener:

- Código.
- Categoría.
- Mensaje seguro.
- Etapa.
- Posibilidad de reintento.
- Correlation ID.
- Detalles técnicos únicamente para logs internos.

## 19. Optimización de costos

La solución deberá ser costo eficiente.

Analiza:

- Costos por página.
- Costos por OCR.
- Costos por modelo.
- Costos por tokens.
- Costos por Lambda.
- Costos por Step Functions.
- Costos por DynamoDB.
- Costos por SQS.
- Costos por almacenamiento temporal.
- Costos por logs.
- Costos por entrenamiento.

Propón mecanismos como:

- No usar IA generativa cuando una regla determinística sea suficiente.
- Ejecutar OCR una sola vez.
- Reutilizar resultados intermedios.
- Aplicar IA solamente a campos ambiguos.
- Seleccionar modelos según complejidad.
- Configurar límites por aplicación.
- Usar TTL.
- Reducir retención de logs.
- Eliminar archivos temporales.
- Procesar páginas relevantes.
- Detener el procesamiento cuando el documento ya fue rechazado.
- Utilizar procesamiento por lotes cuando sea más económico.

Incluye una estimación conceptual de costo por:

- Documento de una página.
- Documento de cinco páginas.
- Documento de veinte páginas.
- Mil documentos mensuales.
- Diez mil documentos mensuales.
- Cien mil documentos mensuales.

Separa costos fijos y variables, sin inventar precios exactos cuando no estén disponibles. Indica qué precios deben consultarse en la calculadora oficial de AWS.

## 20. Infraestructura como código y despliegues

La infraestructura deberá desplegarse automáticamente.

Incluye:

- Ambientes de desarrollo, pruebas y producción.
- Pipeline CI/CD.
- Pruebas automatizadas.
- Despliegues graduales.
- Versiones de Lambda.
- Alias.
- Canary deployments.
- Rollback.
- Variables por ambiente.
- Administración de secretos.
- Migraciones de configuraciones.
- Alarmas posteriores al despliegue.
- Políticas de acceso.
- Validación de infraestructura.

Propón una estructura de repositorio.

Ejemplo:

```text
/src
  /functions
  /domain
  /rules
  /integrations
  /models
  /shared
/infrastructure
/docs
/tests
/examples
/openapi
```

## 21. Documentación requerida

Aunque la solución sea únicamente backend, deberá contar con documentación completa.

Genera una estructura documental que incluya:

1. Introducción.
2. Arquitectura general.
3. Diagrama de componentes.
4. Diagrama de secuencia.
5. Flujo de procesamiento.
6. Registro de una aplicación.
7. Creación de API key.
8. Rotación y revocación de API keys.
9. Configuración de OpenID Connect.
10. Registro de un nuevo tipo de documento.
11. Creación de reglas de extracción.
12. Creación de reglas de validación.
13. Configuración de zonas y coordenadas.
14. Carga de documentos de ejemplo.
15. Entrenamiento de modelos.
16. Envío de documentos.
17. Consulta de resultados.
18. Procesamiento asíncrono.
19. Configuración de webhooks.
20. Manejo de errores.
21. Rate limits.
22. Seguridad.
23. Monitoreo.
24. Costos.
25. Solución de problemas.
26. Ejemplos de integración.
27. OpenAPI/Swagger.
28. Colección de Postman.
29. Guía para desarrolladores.
30. Runbook operativo.

## 22. Fases futuras

Diseña la arquitectura de manera que posteriormente se puedan agregar las siguientes capacidades sin reconstruir completamente la plataforma:

### Respaldo documental

- Guardar documentos originales en Amazon S3.
- Versionado.
- Retención.
- Ciclo de vida.
- Archivado.
- Cifrado.
- Eliminación segura.
- Control de acceso.
- Evidencia de auditoría.

### Búsqueda con RAG

- Indexar documentos y campos extraídos.
- Generar embeddings.
- Buscar información semánticamente.
- Responder preguntas sobre documentos.
- Restringir búsquedas por aplicación.
- Restringir búsquedas por usuario.
- Restringir búsquedas por tipo de documento.
- Mantener aislamiento entre aplicaciones.
- Conservar citas y evidencia.
- Evitar exposición cruzada de información.

## 23. Entregables solicitados

Entrega el resultado en el siguiente orden:

### A. Resumen ejecutivo

Explica la solución de manera comprensible para líderes técnicos y de negocio.

### B. Decisiones y supuestos

Enumera los supuestos realizados y las decisiones que deben validarse.

### C. Arquitectura recomendada

Describe todos los servicios AWS, su responsabilidad y la forma en que se comunican.

### D. Diagramas

Genera diagramas Mermaid para:

- Arquitectura general.
- Flujo síncrono.
- Flujo asíncrono.
- Registro de aplicaciones.
- Registro de documentos.
- Entrenamiento.
- Validación.
- Seguridad.

### E. Modelo de datos

Diseña DynamoDB con claves, índices, entidades y patrones de acceso.

### F. Contratos de API

Genera una especificación inicial OpenAPI 3.1 con ejemplos.

### G. Esquema JSON de configuración

Incluye un ejemplo completo para registrar un tipo de documento y sus reglas.

### H. Flujo de extracción y validación

Explica cada etapa desde la recepción hasta la resolución final.

### I. Seguridad

Incluye autenticación, autorización, cifrado, rate limiting, WAF, auditoría y secretos.

### J. Costos

Identifica los principales generadores de costo y las optimizaciones recomendadas.

### K. Plan de implementación

Divide la construcción en fases o sprints, indicando dependencias y entregables.

### L. Pruebas

Incluye pruebas unitarias, integración, seguridad, rendimiento, precisión, carga y recuperación.

### M. Documentación de integración

Incluye ejemplos para registrar una aplicación, obtener credenciales, registrar un documento, enviar un archivo y consultar resultados.

### N. Riesgos y decisiones pendientes

Señala limitaciones técnicas, riesgos de seguridad, posibles puntos de fallo y decisiones que deben tomarse antes del desarrollo.

## 24. Criterios de diseño

La propuesta deberá priorizar, en este orden:

1. No entregar información incorrecta como válida.
2. Seguridad y aislamiento entre aplicaciones.
3. Trazabilidad.
4. Configuración dinámica.
5. Escalabilidad.
6. Facilidad de integración.
7. Eficiencia de costos.
8. Facilidad de operación.
9. Precisión.
10. Velocidad de procesamiento.

No presentes una solución genérica. Explica las decisiones técnicas, sus ventajas, desventajas, costos y alternativas.

Cuando un requisito sea técnicamente imposible de garantizar de forma absoluta, indícalo claramente y propón controles para reducir el riesgo. Por ejemplo, ningún modelo de inteligencia artificial puede garantizar cero errores; por eso deben utilizarse niveles de confianza, validaciones determinísticas, abstención, revisión humana y evidencia.

Utiliza nombres consistentes, ejemplos realistas y contratos que puedan convertirse posteriormente en una implementación funcional.
s.

- Advertencias.
- Resolución final.
- Evidencia o explicación breve.
- Versión de configuración utilizada.
- Versión del modelo utilizado.

El documento deberá devolver:

- Identificador de procesamiento.
- Aplicación solicitante.
- Tipo de documento detectado o declarado.
- Resolución general.
- Confianza general.
- Campos extraídos.
- Campos validados.
- Errores.
- Advertencias.
- Reglas ejecutadas.
- Tiempo de procesamiento.
- Costo aproximado del procesamiento, cuando sea viable.
- Identificador de trazabilidad.
- Fecha de procesamiento.

## 10. Política contra datos inventados

La plataforma no deberá entregar valores inciertos como si fueran correctos.

Define una política estricta:

1. Si el campo tiene confianza suficiente y supera sus validaciones, devolverlo como validado.
2. Si la confianza es baja, marcarlo como `LOW_CONFIDENCE` o `REVIEW_REQUIRED`.
3. Si el valor no puede determinarse, devolver `NOT_FOUND`.
4. No inventar valores faltantes.
5. No sustituir automáticamente el valor original sin conservar evidencia.
6. Cualquier corrección sugerida por inteligencia artificial deberá devolverse por separado como sugerencia.
7. La respuesta deberá distinguir claramente entre:
   - Valor extraído.
   - Valor normalizado.
   - Valor sugerido.
   - Valor validado.

8. Una sugerencia generada por IA nunca deberá considerarse validada únicamente por haber sido generada.
9. La aplicación consumidora podrá decidir si acepta la sugerencia, solicita revisión o rechaza el documento.
10. Toda inferencia deberá incluir nivel de confianza y explicación.

En casos de baja confianza, se puede utilizar inteligencia artificial para proponer una posible corrección, pero nunca deberá ocultarse que se trata de una inferencia.

Ejemplo:

```json
{
  "extractedValue": "ABCI23",
  "normalizedValue": null,
  "suggestedValue": "ABC123",
  "confidence": 0.61,
  "status": "REVIEW_REQUIRED",
  "suggestionSource": "GENERATIVE_AI",
  "automaticallyAccepted": false
}
```

## 11. Procesamiento síncrono y asíncrono

Debido a que el procesamiento de documentos puede superar el tiempo máximo recomendable de una solicitud HTTP, diseña dos modalidades:

### Procesamiento síncrono

Para documentos pequeños y procesos rápidos:

1. La aplicación envía el documento.
2. El sistema procesa el documento.
3. La misma solicitud devuelve la información extraída y validada.

### Procesamiento asíncrono

Para documentos grandes o procesamiento prolongado:

1. La aplicación envía el documento.
2. El sistema devuelve un identificador de procesamiento.
3. El documento se coloca en una cola.
4. Lambdas o Step Functions realizan el procesamiento.
5. La aplicación consulta el estado mediante una API.
6. Opcionalmente, el sistema notifica el resultado mediante webhook.

Propón criterios configurables para elegir entre procesamiento síncrono y asíncrono.

No mantengas conexiones HTTP abiertas innecesariamente.

## 12. APIs requeridas

Diseña una API REST versionada. Incluye endpoints, contratos, códigos HTTP, validaciones y ejemplos de request y response.

Como mínimo, documenta APIs para:

### Aplicaciones

- Registrar una aplicación.
- Consultar una aplicación.
- Actualizar una aplicación.
- Desactivar una aplicación.
- Regenerar API key.
- Revocar API key.
- Consultar consumo.
- Configurar rate limit.
- Configurar IPs permitidas.
- Configurar webhook.

### Tipos de documentos

- Registrar un tipo de documento.
- Consultar tipos de documentos globales.
- Consultar tipos autorizados para una aplicación.
- Actualizar una configuración.
- Crear una nueva versión.
- Activar una versión.
- Desactivar una versión.
- Configurar campos.
- Configurar reglas.
- Configurar zonas y coordenadas.
- Asociar un documento a una aplicación.

### Procesamiento

- Enviar documento directamente.
- Solicitar una URL prefirmada de carga cuando sea necesario.
- Iniciar procesamiento.
- Consultar estado.
- Consultar resultado.
- Reintentar procesamiento.
- Cancelar procesamiento.
- Descargar evidencia, cuando esté permitida.
- Enviar correcciones.
- Registrar revisión manual.

### Entrenamiento

- Registrar conjunto de documentos.
- Cargar documentos de ejemplo.
- Etiquetar campos.
- Registrar coordenadas.
- Iniciar entrenamiento.
- Consultar estado del entrenamiento.
- Consultar métricas.
- Publicar versión.
- Realizar rollback.

Incluye ejemplos con `curl` y JSON.

## 13. Envío de documentos

Explica y compara las siguientes estrategias:

- `multipart/form-data`.
- Documento codificado en Base64.
- Carga directa a S3 mediante URL prefirmada.
- Referencia a un documento existente.
- Procesamiento por lotes.

Recomienda la estrategia principal considerando:

- Límites de API Gateway y Lambda.
- Tamaño de documentos.
- Seguridad.
- Costo.
- Rendimiento.
- Trazabilidad.
- Facilidad de integración.

No guardar documentos permanentemente en la primera versión, salvo que sea necesario. Define políticas de eliminación automática para archivos temporales.

## 14. Seguridad

La solución deberá incluir una estrategia de seguridad completa.

Considerar:

- OpenID Connect.
- OAuth 2.0 Client Credentials.
- API keys.
- JWT.
- Rotación de credenciales.
- Revocación de credenciales.
- Autorización por aplicación.
- Autorización por tipo de documento.
- Rutas protegidas.
- Cifrado en tránsito.
- Cifrado en reposo.
- AWS KMS.
- AWS Secrets Manager.
- Principio de menor privilegio.
- Roles IAM separados.
- Protección contra DDoS.
- AWS WAF.
- AWS Shield.
- Rate limiting por aplicación.
- Rate limiting por API key.
- Rate limiting por dirección IP.
- Cuotas diarias y mensuales.
- Bloqueo temporal por abuso.
- Validación de archivos.
- Validación de extensión y MIME type.
- Protección contra archivos maliciosos.
- Análisis antivirus o antimalware.
- Prevención de inyección.
- Prevención de NoSQL injection.
- Validación y sanitización de parámetros.
- Límites de tamaño.
- Límites de páginas.
- Protección contra descompresión maliciosa.
- Protección contra cargas repetidas.
- Idempotencia.
- Auditoría.
- Trazabilidad.
- Protección de información sensible.
- Eliminación segura.
- Políticas de retención.
- Parches y vulnerabilidades conocidas en dependencias de Lambda.

Aunque DynamoDB no utiliza SQL tradicional, incluye controles contra inyección, manipulación de expresiones, acceso indebido y entradas maliciosas.

## 15. Rate limiting y control de consumo

Cada aplicación deberá poder tener:

- Solicitudes por segundo.
- Solicitudes por minuto.
- Solicitudes por día.
- Cantidad máxima de documentos.
- Cantidad máxima de páginas.
- Tamaño máximo acumulado.
- Presupuesto máximo mensual.
- Concurrencia máxima.
- Modelos autorizados.
- Tipos de documento autorizados.
- Horarios permitidos.
- IPs permitidas.

El sistema deberá conocer qué aplicación está llamando y aplicar su límite correspondiente.

Explica cómo implementar los límites utilizando API Gateway, AWS WAF, DynamoDB, Lambda, usage plans u otros servicios.

## 16. Datos y modelo de DynamoDB

Diseña el modelo de datos de DynamoDB.

Considera entidades como:

- Aplicaciones.
- Credenciales.
- Tipos de documentos.
- Versiones de documentos.
- Configuraciones.
- Campos.
- Reglas.
- Relación entre aplicaciones y documentos.
- Solicitudes de procesamiento.
- Resultados.
- Auditoría.
- Consumo.
- Rate limits.
- Modelos.
- Entrenamientos.
- Correcciones.
- Webhooks.
- Idempotencia.

Propón:

- Partition keys.
- Sort keys.
- Índices secundarios.
- Patrones de acceso.
- TTL.
- Versionado.
- Bloqueo optimista.
- Estrategia de auditoría.
- Prevención de hot partitions.
- Separación entre configuración y resultados.

No diseñes las tablas como si DynamoDB fuera una base de datos relacional.

## 17. Observabilidad

La solución deberá registrar:

- Aplicación solicitante.
- Tipo de documento.
- Versión.
- Endpoint.
- Estado.
- Tiempo total.
- Tiempo por etapa.
- Número de páginas.
- Modelo utilizado.
- Consumo de tokens, cuando aplique.
- Costo estimado.
- Confianza promedio.
- Número de campos validados.
- Número de campos rechazados.
- Errores.
- Reintentos.
- Mensajes enviados a DLQ.
- Solicitudes bloqueadas.
- Rate limits alcanzados.

Considerar:

- CloudWatch Logs.
- CloudWatch Metrics.
- Alarmas.
- Dashboards.
- X-Ray.
- CloudTrail.
- Correlation ID.
- Trazas distribuidas.
- Dead-letter queues.
- Alertas por errores o incremento de costos.

No registrar documentos completos ni información sensible innecesariamente en los logs.

## 18. Manejo de errores

Diseña una estrategia para:

- Reintentos.
- Backoff exponencial.
- Dead-letter queues.
- Errores parciales.
- Timeouts.
- Fallos de modelos.
- Fallos de OCR.
- Fallos de reglas externas.
- Documentos corruptos.
- Formatos no admitidos.
- Documentos protegidos por contraseña.
- Documentos ilegibles.
- Procesamiento duplicado.
- Webhooks fallidos.
- Recuperación de procesos detenidos.
- Idempotencia.

Cada error deberá tener:

- Código.
- Categoría.
- Mensaje seguro.
- Etapa.
- Posibilidad de reintento.
- Correlation ID.
- Detalles técnicos únicamente para logs internos.

## 19. Optimización de costos

La solución deberá ser costo eficiente.

Analiza:

- Costos por página.
- Costos por OCR.
- Costos por modelo.
- Costos por tokens.
- Costos por Lambda.
- Costos por Step Functions.
- Costos por DynamoDB.
- Costos por SQS.
- Costos por almacenamiento temporal.
- Costos por logs.
- Costos por entrenamiento.

Propón mecanismos como:

- No usar IA generativa cuando una regla determinística sea suficiente.
- Ejecutar OCR una sola vez.
- Reutilizar resultados intermedios.
- Aplicar IA solamente a campos ambiguos.
- Seleccionar modelos según complejidad.
- Configurar límites por aplicación.
- Usar TTL.
- Reducir retención de logs.
- Eliminar archivos temporales.
- Procesar páginas relevantes.
- Detener el procesamiento cuando el documento ya fue rechazado.
- Utilizar procesamiento por lotes cuando sea más económico.

Incluye una estimación conceptual de costo por:

- Documento de una página.
- Documento de cinco páginas.
- Documento de veinte páginas.
- Mil documentos mensuales.
- Diez mil documentos mensuales.
- Cien mil documentos mensuales.

Separa costos fijos y variables, sin inventar precios exactos cuando no estén disponibles. Indica qué precios deben consultarse en la calculadora oficial de AWS.

## 20. Infraestructura como código y despliegues

La infraestructura deberá desplegarse automáticamente.

Incluye:

- Ambientes de desarrollo, pruebas y producción.
- Pipeline CI/CD.
- Pruebas automatizadas.
- Despliegues graduales.
- Versiones de Lambda.
- Alias.
- Canary deployments.
- Rollback.
- Variables por ambiente.
- Administración de secretos.
- Migraciones de configuraciones.
- Alarmas posteriores al despliegue.
- Políticas de acceso.
- Validación de infraestructura.

Propón una estructura de repositorio.

Ejemplo:

```text
/src
  /functions
  /domain
  /rules
  /integrations
  /models
  /shared
/infrastructure
/docs
/tests
/examples
/openapi
```

## 21. Documentación requerida

Aunque la solución sea únicamente backend, deberá contar con documentación completa.

Genera una estructura documental que incluya:

1. Introducción.
2. Arquitectura general.
3. Diagrama de componentes.
4. Diagrama de secuencia.
5. Flujo de procesamiento.
6. Registro de una aplicación.
7. Creación de API key.
8. Rotación y revocación de API keys.
9. Configuración de OpenID Connect.
10. Registro de un nuevo tipo de documento.
11. Creación de reglas de extracción.
12. Creación de reglas de validación.
13. Configuración de zonas y coordenadas.
14. Carga de documentos de ejemplo.
15. Entrenamiento de modelos.
16. Envío de documentos.
17. Consulta de resultados.
18. Procesamiento asíncrono.
19. Configuración de webhooks.
20. Manejo de errores.
21. Rate limits.
22. Seguridad.
23. Monitoreo.
24. Costos.
25. Solución de problemas.
26. Ejemplos de integración.
27. OpenAPI/Swagger.
28. Colección de Postman.
29. Guía para desarrolladores.
30. Runbook operativo.

## 22. Fases futuras

Diseña la arquitectura de manera que posteriormente se puedan agregar las siguientes capacidades sin reconstruir completamente la plataforma:

### Respaldo documental

- Guardar documentos originales en Amazon S3.
- Versionado.
- Retención.
- Ciclo de vida.
- Archivado.
- Cifrado.
- Eliminación segura.
- Control de acceso.
- Evidencia de auditoría.

### Búsqueda con RAG

- Indexar documentos y campos extraídos.
- Generar embeddings.
- Buscar información semánticamente.
- Responder preguntas sobre documentos.
- Restringir búsquedas por aplicación.
- Restringir búsquedas por usuario.
- Restringir búsquedas por tipo de documento.
- Mantener aislamiento entre aplicaciones.
- Conservar citas y evidencia.
- Evitar exposición cruzada de información.

## 23. Entregables solicitados

Entrega el resultado en el siguiente orden:

### A. Resumen ejecutivo

Explica la solución de manera comprensible para líderes técnicos y de negocio.

### B. Decisiones y supuestos

Enumera los supuestos realizados y las decisiones que deben validarse.

### C. Arquitectura recomendada

Describe todos los servicios AWS, su responsabilidad y la forma en que se comunican.

### D. Diagramas

Genera diagramas Mermaid para:

- Arquitectura general.
- Flujo síncrono.
- Flujo asíncrono.
- Registro de aplicaciones.
- Registro de documentos.
- Entrenamiento.
- Validación.
- Seguridad.

### E. Modelo de datos

Diseña DynamoDB con claves, índices, entidades y patrones de acceso.

### F. Contratos de API

Genera una especificación inicial OpenAPI 3.1 con ejemplos.

### G. Esquema JSON de configuración

Incluye un ejemplo completo para registrar un tipo de documento y sus reglas.

### H. Flujo de extracción y validación

Explica cada etapa desde la recepción hasta la resolución final.

### I. Seguridad

Incluye autenticación, autorización, cifrado, rate limiting, WAF, auditoría y secretos.

### J. Costos

Identifica los principales generadores de costo y las optimizaciones recomendadas.

### K. Plan de implementación

Divide la construcción en fases o sprints, indicando dependencias y entregables.

### L. Pruebas

Incluye pruebas unitarias, integración, seguridad, rendimiento, precisión, carga y recuperación.

### M. Documentación de integración

Incluye ejemplos para registrar una aplicación, obtener credenciales, registrar un documento, enviar un archivo y consultar resultados.

### N. Riesgos y decisiones pendientes

Señala limitaciones técnicas, riesgos de seguridad, posibles puntos de fallo y decisiones que deben tomarse antes del desarrollo.

## 24. Criterios de diseño

La propuesta deberá priorizar, en este orden:

1. No entregar información incorrecta como válida.
2. Seguridad y aislamiento entre aplicaciones.
3. Trazabilidad.
4. Configuración dinámica.
5. Escalabilidad.
6. Facilidad de integración.
7. Eficiencia de costos.
8. Facilidad de operación.
9. Precisión.
10. Velocidad de procesamiento.

No presentes una solución genérica. Explica las decisiones técnicas, sus ventajas, desventajas, costos y alternativas.

Cuando un requisito sea técnicamente imposible de garantizar de forma absoluta, indícalo claramente y propón controles para reducir el riesgo. Por ejemplo, ningún modelo de inteligencia artificial puede garantizar cero errores; por eso deben utilizarse niveles de confianza, validaciones determinísticas, abstención, revisión humana y evidencia.

Utiliza nombres consistentes, ejemplos realistas y contratos que puedan convertirse posteriormente en una implementación funcional.
