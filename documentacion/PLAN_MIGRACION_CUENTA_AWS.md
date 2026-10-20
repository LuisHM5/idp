# Plan de migración de cuenta AWS, respaldo y atribución de costos

Para ejecutar la migración con una secuencia corta de acciones y señales de
éxito, empezar por la [guía simple](GUIA_MIGRACION_CUENTA_AWS.md).

> Documento de traspaso para continuar el trabajo desde el repositorio
> `documents-validator`. No contiene secretos ni autoriza por sí mismo cambios en
> AWS. Toda operación de escritura, despliegue o corte debe revisarse y aprobarse
> antes de ejecutarse.

## 1. Objetivo y decisiones confirmadas

Se debe migrar el GDI/IDP desde la cuenta AWS actual hacia una cuenta AWS
independiente, conservando los datos y las estadísticas de uso. Antes de retirar
la cuenta origen, conservar dos copias independientes:

1. Un respaldo local cifrado y portable.
2. Una copia del mismo archivo cifrado en otro disco o en la cuenta AWS nueva.

Decisiones tomadas durante el análisis:

- Se migran **producción y desarrollo**.
- La cuenta destino **no pertenece a la misma AWS Organization** que la cuenta
  actual. La ruta operativa usa lectura local de DynamoDB y escritura directa
  en destino; AWS Backup cross-account no se considera requisito.
- Se acepta una **ventana breve sin nuevas escrituras** para el corte final.
- Los reportes deben poder seleccionar `prod`, `dev` o ambos.
- El costo se debe atribuir solamente a recursos definidos o referenciados por
  la infraestructura como código de cada ambiente, incluso cuando no haya tags
  disponibles.
- Nunca se debe sumar el costo completo de un servicio de la cuenta compartida
  solo porque el proyecto también utilice ese servicio.

## 2. Repositorio y arquitectura relevantes

Este es el repositorio correcto para implementar el trabajo:

- Ruta local: `/home/luis-pc/Desktop/documents-validator`
- CDK entrypoint: `bin/app.ts`
- Stack principal: `src/document-validation-stack.ts`
- Construct de almacenamiento y procesamiento:
  `src/document-processing/multi-model-document-processing.ts`
- Reporte unificado: `scripts/reporte.py` (`pnpm reporte`)
- Cálculo actual de infraestructura: `scripts/infrastructure_costs.py`
- Utilidades comunes de reportes: `scripts/_report_lib.py`
- Auditoría por documento:
  `src/document-processing/resources/enrichment-lambda/cost_audit.py`

Stacks desplegados observados en `us-east-1`:

| Ambiente | Stack |
|---|---|
| Producción | `DocumentValidationStack` |
| Desarrollo | `DocumentValidationStack-Dev` |

La arquitectura actual del código usa ingreso directo S3 → Enrichment Lambda.
No debe reconstruirse deliberadamente la arquitectura histórica de SQS + Step
Functions. El stack productivo observado todavía contiene recursos históricos,
por lo que antes de migrar es obligatorio comparar el CDK actual con ambos
despliegues y decidir qué recursos son vigentes, cuáles contienen datos y cuáles
solo son residuos de versiones anteriores.

## 3. Estado observado en la cuenta origen

La inspección fue de solo lectura. No se activaron backups, no se modificaron
tablas y no se consultaron valores secretos.

### 3.1 DynamoDB

Se encontraron estas tablas:

| Tabla | Función | Estado de protección observado |
|---|---|---|
| `DocumentValidationStack-BedrockDocumentProcessingDocumentProcessingTable...` | Seguimiento y resultado de documentos prod | PITR activo; sin deletion protection |
| `DocumentValidationStack-Dev-BedrockDocumentProcessingDocumentProcessingTable...` | Seguimiento y resultado de documentos dev | PITR activo; sin deletion protection |
| `document-validation-costs` | Auditoría de costos por documento prod | Sin PITR; `RETAIN` en la plantilla desplegada |
| `document-validation-costs-dev` | Auditoría de costos por documento dev | Sin PITR; `RETAIN` |
| `document-validation-apps` | Registro de aplicaciones/API keys prod | Sin PITR; `DESTROY` |
| `document-validation-apps-dev` | Registro de aplicaciones/API keys dev | Sin PITR; `DESTROY` |
| `document-validation-app-usage-dev` | Contador diario por API key dev | Sin PITR; TTL `ExpiresAt`; `DESTROY` |
| `document-validation-keys` | Índice derivado de claves únicas prod | Sin PITR; `DESTROY` |
| `document-validation-keys-dev` | Índice derivado de claves únicas dev | Sin PITR; `DESTROY` |
| `document-validation-catalog` | Overrides de catálogo prod | Sin PITR; importada por nombre, fuera del stack |
| `document-validation-catalog-dev` | Overrides de catálogo dev | Sin PITR; importada por nombre, fuera del stack |
| `document-validation-fingerprints-dev` | Memoria y referencias de autenticidad dev | Sin PITR; TTL solo para reservas pendientes; `RETAIN` |

No había backups DynamoDB bajo demanda. Todas las tablas tenían deletion
protection desactivada.

El CDK actual ya declara `FingerprintTable` y `AppUsageTable` para ambos
ambientes, además de una tabla compartida opcional de fallback de modelos. La
ausencia de algunas de esas tablas en producción demuestra que el código y el
stack desplegado no están completamente alineados. El inventario debe repetirse
inmediatamente antes de la migración.

#### Datos que no son equivalentes

- `CostTable` es el histórico financiero/operativo por documento. Conserva
  `AppOrigin`, tipo documental, páginas, tokens, desglose de servicios,
  `TotalCostUsd`, versión de precios y timestamps. No tiene TTL y debe tratarse
  como información de auditoría.
- `AppUsageTable` es un contador para aplicar el límite diario por API key. Sus
  filas expiran a los 90 días. No es una fuente histórica permanente.
- `UniqueKeyTable` es derivada y puede reconstruirse con `pnpm backfill:keys`,
  pero debe copiarse para reducir riesgo y tiempo de recuperación.
- `FingerprintTable` mezcla historia de autenticidad y referencias registradas
  manualmente. No todo puede reconstruirse; debe tratarse como dato crítico.
- Las tablas de catálogo se importan con `fromTableName`; que no pertenezcan a
  CloudFormation no significa que estén fuera del proyecto.

Solo puede respaldarse el historial todavía presente. Las filas que DynamoDB ya
eliminó por TTL no pueden recuperarse.

### 3.2 S3, S3 Vectors y KMS

Se observaron dos buckets de documentos, uno por ambiente:

- Producción: aproximadamente 13 objetos y 17.6 MB.
- Desarrollo: aproximadamente 391 objetos y 205.7 MB.

Ambos estaban sin versionado. La plantilla desplegada usa `DESTROY` y
`autoDeleteObjects` para estos buckets. Producción usa una CMK; desarrollo usa
SSE-S3. El CDK actual conserva el mismo comportamiento destructivo por defecto.

Desarrollo también tiene un bucket/índice S3 Vectors. Ese índice es derivado:
los vectores viven igualmente en las filas activas de `FingerprintTable`, y
`pnpm refs:reindex` puede reconstruir el índice. Las imágenes originales de las
referencias viven bajo `references/` en el bucket documental y sí son críticas.

Una clave KMS no se migra entre cuentas. La copia de objetos deberá leer con la
clave origen y escribir cifrada con una nueva clave de la cuenta destino.

### 3.3 CloudFormation y eliminación accidental

- Los dos stacks tenían termination protection desactivada.
- El stack productivo llevaba el tag `CostStack=DocumentValidationStack`.
- El stack dev llevaba `CostStack=DocumentValidationStack-Dev`.
- La CMK productiva tenía retención, pero varios recursos con datos seguían con
  políticas destructivas.
- Los grupos de logs explícitos observados tenían retención de dos años, pero
  la plantilla permite eliminarlos con el stack.

### 3.4 Costos y reportes

No existía ninguna definición de CUR clásico ni de AWS Data Exports/CUR 2.0.
Por ello no hay un histórico de facturación con IDs de recursos disponible para
reconstruir retroactivamente todos los costos exactos.

El reporte actual ya hace bastante trabajo que debe conservarse:

- `pnpm reporte` combina las tablas de documentos y costos.
- Sin `--env`, lee prod y dev, etiqueta cada fila con su ambiente y permite
  filtrar el HTML.
- `collect_infrastructure_costs()` obtiene los recursos físicos mediante
  `ListStackResources`.
- Primero intenta Cost Explorer filtrado por `CostStack`.
- Si el tag de costo no está disponible, consulta costos por servicio de toda
  la cuenta, pero correctamente no los suma al stack sin evidencia.
- Solo atribuye actualmente cargos reales de WAF/KMS sin tags cuando demuestra
  que todos los recursos facturables de esa clase en la cuenta pertenecen al
  stack.
- Los demás servicios de pago por uso se muestran como inventario/contexto,
  no como costo exacto del stack.

El `CostTable` calcula el costo técnico de cada documento usando consumo real
de tokens/páginas y tarifas mantenidas por el proyecto. Es más granular para el
flujo del documento, pero no reemplaza la factura AWS ni incluye por sí sola
todos los costos de infraestructura.

## 4. Resultado esperado

Al terminar, deben existir:

1. Un paquete local cifrado y autocontenido para prod y dev.
2. Una copia equivalente en un medio independiente; el bucket de transferencia
   de destino es una opción.
3. Los datos restaurados y validados en la cuenta nueva.
4. Infraestructura CDK protegida contra eliminación accidental.
5. CUR 2.0 consultable con Athena.
6. `pnpm reporte` capaz de mostrar solamente los costos atribuibles a los
   recursos/principales de `prod`, `dev` o ambos, sin depender de tags.
7. Una clasificación visible del costo: directo, por principal, estimado o no
   atribuible.
8. Un procedimiento de reversión que conserve intacta la cuenta origen hasta
   aprobar el nuevo ambiente.

## 5. Fase A: reconciliar y proteger la infraestructura

Antes de generar el respaldo final:

1. Ejecutar `pnpm build` y sintetizar ambos ambientes.
2. Ejecutar `cdk diff` para prod y dev sin desplegar.
3. Comparar el inventario del template sintetizado con
   `cloudformation list-stack-resources`.
4. Identificar expresamente recursos importados por nombre:
   - `document-validation-catalog[-dev]`.
   - Tabla compartida de fallback de modelos, si existe.
   - Certificado ACM y DNS externos.
   - API keys/usage plans creados por `pnpm apps:register`.
5. No eliminar recursos históricos hasta demostrar que no contienen datos ni
   participan en el flujo vigente.

Protecciones adicionales pendientes para datos no reconstruibles:

- Activar PITR en `CostTable`, `AppsTable`, `AppUsageTable`, `FingerprintTable`
  y tablas de catálogo/fallback administradas fuera del stack.
- Añadir deletion protection a las tablas críticas.
- Usar `RemovalPolicy.RETAIN` y `UpdateReplacePolicy.RETAIN` para tablas y
  buckets críticos.
- Retirar `autoDeleteObjects` de buckets de documentos.
- Activar versionado en los buckets de documentos.
- Activar termination protection para ambos stacks en el pipeline de deploy.
- Conservar `UniqueKeyTable` como reconstruible, pero incluirla en el respaldo.
- Mantener TTL en `AppUsageTable`: es control operativo. Los reportes históricos
  deben leer `CostTable`, no depender de esos contadores temporales.

`migration:deploy` no aplica por sí solo estas protecciones: usa las políticas
de los stacks actuales. Implementarlas y comprobar con `cdk diff` que no
reemplazan tablas ni buckets antes de retirar la cuenta origen. La ruta local
de respaldo y restauración no depende de que PITR o versionado estén activos.

## 6. Fase B: respaldo local y respaldo de transferencia

### 6.0 Comandos operativos

La CLI `scripts/aws_migration.py` ejecuta el inventario, lee directamente las
tablas DynamoDB con `Scan` consistente y paginado, descarga los objetos actuales
de ambos buckets documentales,
crea un manifiesto con SHA-256 por archivo y cifra el paquete con OpenSSL y una
contraseña elegida durante el asistente. Requiere `pnpm install`,
`pnpm setup:python`, OpenSSL instalado y credenciales AWS para cada cuenta. No
requiere crear claves públicas. No necesita buckets temporales en origen ni
destino. Los buckets documentales propios de la aplicación sí deben existir en
destino. La segunda copia cifrada puede guardarse en otro medio local o en un
bucket de transferencia del destino.

```bash
pnpm migration:plan       # Inventario de tablas y buckets; solo lectura.
pnpm migration:deploy     # Fase Inicial: CDK destino con ingreso S3 pausado.
pnpm migration:backup     # Asistente para crear el respaldo cifrado.
pnpm migration:restore    # Carga datos en los stacks CDK de destino.
pnpm migration:deploy     # Fase Activar: habilita ingreso S3 después de restaurar.
```

También funcionan con npm: `npm run migration:plan`,
`npm run migration:deploy`, `npm run migration:backup` y
`npm run migration:restore`. En los selectores,
usar ↑/↓ y Enter; las rutas aceptan Tab para autocompletar. Los campos
inválidos muestran el motivo y permiten volver a escribirlos. La interfaz de
colores y progreso vive en `scripts/terminal_ui.py` para reutilizarla desde
otros comandos del repositorio; `NO_COLOR=1` desactiva los colores.

Los cuatro comandos requieren una terminal interactiva (TTY), preguntan los
datos necesarios y rechazan la ejecución desatendida. `backup` y `restore`
muestran un resumen y exigen escribir `RESPALDAR <cuenta>` o
`RESTAURAR <cuenta>` antes de la primera escritura AWS. `plan` solicita
`CONSULTAR <perfil>` antes de consultar AWS. Los argumentos opcionales pueden
precargar respuestas, pero nunca omiten las preguntas ni la confirmación.
`migration:deploy` pide el perfil, fase y ambiente; exige escribir
`DESPLEGAR <cuenta> PAUSADO` o `DESPLEGAR <cuenta> ACTIVO` antes de invocar CDK.
Usar `plan` primero; detener las escrituras al ejecutar `backup` para el corte
final. El asistente muestra los perfiles AWS configurados y confirma el ID de
cuenta mediante STS antes de continuar: **un ID numérico de cuenta no es el
nombre del perfil**. `actual` utiliza las credenciales activas del shell, rol
o perfil default. En `backup` se preguntan perfil y región de origen, ambiente,
una contraseña nueva de al menos 12 caracteres, archivo de salida, directorio
temporal y, de forma opcional, una segunda ubicación local o un bucket/perfil
de transferencia.
El archivo cifrado propone por defecto
`~/aws-migration-backup-AAAAmmdd-HHMMSS.tar.gz.enc`; la carpeta temporal
propone `/tmp` (o el directorio temporal configurado en el sistema). Basta
pulsar Enter para aceptarlos. Durante el respaldo y la restauración habrá
datos sin cifrar en esa carpeta temporal, que se elimina al terminar; `/tmp`
puede residir en un disco sin cifrar y debe tener espacio suficiente. Se puede
elegir otra carpeta existente en un volumen cifrado.
La contraseña se pide de nuevo al restaurar; no se guarda en el manifiesto ni
se muestra mientras se escribe. Guardarla en un gestor de contraseñas: sin ella
no se podrá abrir el respaldo.

Ejemplo de respuestas para `pnpm migration:backup`:

| Pregunta | Qué escribir |
|---|---|
| Perfil AWS de origen | Elegir con ↑/↓ un perfil configurado, por ejemplo `alsuper`, o `actual` si las credenciales activas son las correctas. Nunca el ID de cuenta `886031931562`. |
| Región | `us-east-1` si allí está desplegado el stack; Enter acepta el valor entre corchetes. |
| Ambiente | Elegir con ↑/↓: ambos (`both`), producción (`prod`) o desarrollo (`dev`). |
| Contraseña | Una contraseña nueva, larga y única; se escribe dos veces sin mostrarse. No es una clave GPG ni una contraseña AWS. |
| Ruta del archivo | Enter acepta la ruta sugerida en tu carpeta personal; también puedes escribir `/mnt/respaldo/idp-2026-10-19.tar.gz.enc`. |
| Carpeta temporal | Enter acepta `/tmp`; usa otra carpeta existente si necesitas más espacio o un volumen cifrado. |
| Segunda copia | Enter omite esta copia en la ejecución actual. Si la haces, usa otro disco o un bucket de transferencia de la cuenta destino. |

El asistente consulta STS y muestra la cuenta real del perfil elegido **antes**
de pedir la contraseña. Verificar que coincide con la cuenta origen esperada.

La copia adicional es el mismo archivo cifrado, byte por byte: no contiene
datos distintos. El asistente permite omitirla, pero el plan de corte final
requiere dos copias independientes antes de retirar la cuenta origen. Una
segunda ruta dentro del mismo sistema de archivos, como `~/Downloads/output`
si el primer archivo también está en `~`, ayuda ante un borrado accidental,
pero no ante una falla de ese disco. El asistente avisa si detecta que ambas
rutas están en el mismo sistema de archivos. El manifiesto registra hora de
inicio y fin, no afirma un snapshot puntual. AWS advierte que `Scan`, incluso
con lectura consistente, no ofrece aislamiento de snapshot: **detener todas
las escrituras de DynamoDB y S3 antes de iniciar el respaldo y mantenerlas
detenidas hasta que termine**. S3 conserva solo la versión actual de cada
objeto, no el historial de versiones. El `Scan` consume capacidad de lectura;
PITR no es requisito de esta ruta, aunque sigue recomendado como protección.

Para restaurar en la ruta recomendada, primero ejecutar `migration:deploy` en
fase **Inicial**. CDK crea buckets y tablas con cifrado de la cuenta nueva,
pero sin notificaciones S3. `restore` pregunta la ruta del respaldo, el
directorio temporal y el perfil destino. En la opción predeterminada **Stacks
CDK ya desplegados**, asocia los recursos por ID lógico de CloudFormation,
descifra y verifica todos los SHA-256, comprueba que tablas y buckets estén
vacíos, que las claves/GSI coincidan y que no haya notificaciones. Muestra el
mapa físico y solicita la confirmación final antes de `BatchWriteItem`/carga
S3. Si falta algún dato o falla esa revisión, no escribe en destino.

El modo alternativo **Tablas nuevas fuera de CDK** conserva la restauración
aislada anterior: requiere crear buckets destino vacíos manualmente y permite
indicar un mapa local JSON con `--bucket-map`, por ejemplo
`{"prod":"bucket-documentos-prod-nuevo","dev":"bucket-documentos-dev-nuevo"}`.
Exige que las tablas destino no existan y puede requerir el ARN de una CMK
destino. **Ese modo no debe preceder a un deploy normal**: el stack crearía
recursos paralelos o chocaría con tablas del mismo nombre.

Con un respaldo ya creado, iniciar el asistente así (también sirve la copia
idéntica del archivo cifrado):

```bash
pnpm migration:restore --archive ~/aws-migration-backup-AAAAmmdd-HHMMSS.tar.gz.enc
```

El nombre del archivo se propone en pantalla; completar los demás campos de
forma interactiva. El perfil destino debe resolver a otra cuenta AWS.

La restauración activa PITR y TTL según el origen y habilita deletion protection
en cada tabla. En modo stacks usa la CMK creada por CDK en destino; en modo
tablas nuevas usa la CMK indicada para prod. Conserva el cifrado del bucket
destino al subir los objetos; la CMK origen nunca se reutiliza. No migra los
valores secretos de API Gateway, no reconstruye S3 Vectors ni cambia el
tráfico. Esas acciones siguen en las fases C y D. Si una carga falla, deja los
recursos para inspección; no borra datos ni reintenta sobre tablas no vacías.
Mantener espacio
local suficiente para el paquete descifrado durante la operación; el directorio
temporal se elimina al terminar y debe estar sobre almacenamiento cifrado.

### 6.1 Ventana sin escrituras

Registrar el inicio y el fin UTC del respaldo. La CLI usa `Scan` paginado con
`ConsistentRead=true` para cada tabla y guarda sus items en DynamoDB JSON local.
También guarda esquema, índices, TTL y estado PITR por separado. Un `Scan` no
ofrece una vista atómica de toda la tabla; la ventana sin escrituras es la que
permite tratar los resultados como un corte lógico común. Confirmar que no haya
documentos en proceso ni expiraciones TTL relevantes durante la ventana. Si no
se puede detener la escritura, usar la ruta alternativa Export to S3 con PITR.

### 6.2 Copia local

Leer las tablas y descargar los objetos a un directorio temporal en un volumen
cifrado. El paquete `.enc` conserva esta estructura tras descifrarse:

```text
backup/
  manifest.json
  cloudformation/<environment>/template.json
  cloudformation/<environment>/resources.json
  dynamodb/<environment>/<table>/
    schema.json
    ttl.json
    pitr.json
    items.jsonl.gz
  s3/<environment>/<object-key>
```

`manifest.json` debe incluir cuenta/region de origen, stack, ambiente, hora de
inicio/fin, tabla física, número de items leídos, metadata S3 y hashes SHA-256.
No debe incluir valores de API keys,
credenciales ni contenido de variables `.env`.

El paquete local debe cifrarse antes de salir del equipo y probarse abriendo una
copia en un directorio temporal.

### 6.3 Copia de transferencia

El asistente copia el archivo cifrado a otro medio local o lo carga a un bucket de
transferencia del destino.
Esta copia no es un segundo muestreo de las tablas. Restaurar los objetos
documentales con los buckets y claves de cifrado nuevos: ninguna dependencia
de la CMK origen debe permanecer.

### 6.4 API keys y configuración externa

El `ApiKeyId` guardado en `AppsTable` no contiene el valor secreto de la API
key. Para evitar romper consumidores:

- Exportar los valores actuales por un canal cifrado y fuera del repositorio.
- Crear API keys en destino con los mismos valores cuando AWS lo permita.
- Crear nuevos usage plans y actualizar `AppsTable` con los nuevos IDs.
- Validar permisos como `AccessScope`, `AllowFieldSearch`,
  `AllowMetadataWrite`, `AllowReferenceWrite` y límites diarios.

También deben documentarse/recrearse ACM, DNS, SNS subscriptions y cualquier
configuración externa. Nunca almacenar secretos o certificados privados en este
archivo ni en Git.

## 7. Fase C: restauración en la cuenta independiente

### 7.1 Dos fases CDK y restauración sobre recursos administrados

La ruta recomendada **despliega primero** el CDK en la cuenta nueva con el
ingreso S3 pausado. `pnpm migration:deploy` verifica la identidad AWS elegida
de forma interactiva, ejecuta `deploy:dev` y `deploy:prod` con ese perfil y
`MIGRATION_PAUSE_INGRESS=1`, y los stacks crean KMS, tablas, buckets, Lambda,
API Gateway, EventBridge, IAM y demás recursos. El construct conserva las
notificaciones `source/`/`raw/` desactivadas en esta fase: así la carga de
objetos históricos no vuelve a procesarlos.

Después, `pnpm migration:restore` en el modo predeterminado **Stacks CDK ya
desplegados** descifra y verifica el respaldo, asocia cada tabla y bucket del
origen con el mismo ID lógico en los stacks nuevos, comprueba que las tablas y
buckets estén vacíos, y carga los datos allí. Las tablas importadas por nombre
fuera del stack (catálogo/fallback) conservan su nombre y se cargan o crean si
faltan. La CMK productiva es la que creó el CDK de la cuenta nueva; no se copia
la CMK origen ni hace falta escribir un ARN para este modo. El asistente sigue
ofreciendo el modo **Tablas nuevas fuera de CDK** para recuperación de datos
aislada, pero ese modo no prepara la aplicación para desplegarse.

Por último, `pnpm migration:deploy` en fase **Activar** vuelve a desplegar los
mismos stacks sin `MIGRATION_PAUSE_INGRESS`, habilitando solo las notificaciones
S3 de `source/` y `raw/`. Ese paso no crea otro bucket ni otra tabla: actualiza
la configuración de los buckets ya administrados por CloudFormation.

No invertir este orden. Un `pnpm run deploy:prod` normal **antes** de cargar
datos crea notificaciones que procesarían de nuevo los objetos `source/` al
subirlos. Un `migration:restore` en el modo **tablas nuevas** seguido de
`deploy:prod` puede chocar con tablas de nombre fijo y dejar la aplicación
apuntando a una tabla de seguimiento o bucket vacíos creados por CDK.

### 7.2 Secuencia para habilitar el despliegue en la cuenta destino

1. **Identidad y configuración.** Configurar un perfil de la cuenta nueva y
   preparar `.env.dev`/`.env.production` para ella. Usar un certificado ACM de
   destino o dejar `DOMAIN_NAME`/`CERT_ARN` sin configurar hasta el corte.
   Habilitar acceso a los modelos Bedrock en la región destino. El asistente
   muestra el ID de cuenta mediante STS y exige confirmarlo por escrito.
2. **CDK inicial sin ingreso.** Ejecutar `pnpm migration:deploy`, seleccionar
   el perfil destino, `both` y la fase **Inicial**. El asistente limpia posibles
   credenciales AWS de entorno que podrían prevalecer sobre ese perfil y llama
   a los scripts `deploy:dev`/`deploy:prod` con el ingreso pausado. Comprobar
   que ambos stacks quedaron `CREATE_COMPLETE`/`UPDATE_COMPLETE` y que sus
   buckets no tienen notificaciones.
3. **Datos, durante el corte final.** Con escrituras detenidas en origen,
   ejecutar el respaldo definitivo y `pnpm migration:restore` en modo
   **Stacks CDK ya desplegados**. Confirmar el mapa físico origen → destino,
   la ausencia de datos previos en tablas/buckets y el ID de la cuenta nueva.
   El comando no sobrescribe recursos no vacíos; no hacer ensayos sobre el
   mismo destino que se usará para el corte final.
4. **Activación.** Ejecutar otra vez `pnpm migration:deploy` con el mismo
   perfil, `both` y la fase **Activar**. Revisar los cambios CDK: solo deben
   agregarse las notificaciones `source/`/`raw/` a los buckets de los mismos
   stacks, sin reemplazar tablas ni buckets. Confirmar que los documentos
   históricos no son reprocesados.
5. **Recursos externos y validación.** Recrear API keys/usage plans en destino
   y reconciliar `AppsTable` con los nuevos `ApiKeyId`; configurar ACM/DNS,
   secretos y suscripciones. Reconstruir S3 Vectors con `pnpm refs:reindex`
   después de restaurar `FingerprintTable` y `references/`. Ejecutar
   `pnpm backfill:index`/`pnpm backfill:keys` solo si hace falta. Completar las
   comprobaciones de la sección 10 antes de cambiar el tráfico.

`pnpm run deploy:prod` continúa siendo el comando de despliegue normal para
una cuenta ya operativa. En una migración, usar `pnpm migration:deploy` para
elegir explícitamente perfil y fase. Ninguno de los dos mueve automáticamente
valores secretos de API keys, ACM, DNS ni configuración fuera de IaC.

## 8. Fase D: corte con ventana breve

El despliegue CDK inicial del punto 7.2 se hace **antes** de esta ventana, con
el ingreso S3 pausado y sin tráfico de consumidores. Lo siguiente ejecuta una
sola restauración final, nunca una segunda carga sobre las mismas tablas.

1. Desactivar temporalmente las rutas que crean, reprocesan o modifican
   documentos/referencias; las lecturas pueden continuar desde origen.
2. Esperar a que no existan documentos en procesamiento ni invocaciones
   asíncronas pendientes.
3. Registrar el inicio de la ventana y ejecutar el respaldo local definitivo
   mientras las escrituras permanecen detenidas.
4. Verificar que el respaldo final contiene las tablas/objetos esperados y
   conservar su SHA-256 y contraseña.
5. Ejecutar `pnpm migration:restore` en modo **Stacks CDK ya desplegados**:
   cargar tablas y objetos en los recursos vacíos de los stacks nuevos.
6. Ejecutar `pnpm migration:deploy` en fase **Activar** y comprobar que solo
   se habilitan las notificaciones esperadas, sin reemplazo de datos.
7. Recrear API keys/usage plans, comprobar autenticación y completar la
   validación de la sección 10.
8. Cambiar el dominio/endpoint solo cuando todas las pruebas sean satisfactorias.
9. Mantener origen sin escrituras, pero disponible para rollback.
10. Si falla una validación crítica, revertir DNS/configuración del consumidor a
   origen; no intentar reparar datos en producción durante el corte.

## 9. Atribución de costos sin depender de tags

### 9.1 Fuente de facturación

Configurar en la cuenta pagadora/destino un AWS Data Export de CUR 2.0 con:

- Granularidad diaria como mínimo; horaria si el volumen/costo es aceptable.
- `INCLUDE_RESOURCES=TRUE` para obtener `line_item_resource_id`.
- `INCLUDE_IAM_PRINCIPAL_DATA=TRUE` para atribuir inferencia Bedrock al rol que
  hizo la llamada.
- Entrega versionada a S3 y catálogo/tabla Athena.
- `UnblendedCost` como métrica principal.
- Créditos, reembolsos e impuestos visibles por separado, no mezclados con el
  costo operativo atribuible.

CUR 2.0 solo aportará granularidad desde que se active. El histórico previo se
conserva mediante `CostTable` y Cost Explorer disponible, con una etiqueta de
procedencia/certeza; no debe presentarse como factura por recurso si AWS nunca
generó esos datos.

### 9.2 Inventario IaC fechado

Extender `scripts/infrastructure_costs.py`; no crear un segundo reporte.

El inventario por ambiente debe combinar:

1. Recursos propiedad del stack mediante `ListStackResources`.
2. Recursos importados/referenciados por IaC, como catálogos y fallback models.
3. Roles IAM de las Lambdas del stack.
4. Alias facturables: ARN, nombre físico, ID corto, nombre de función Lambda,
   nombre de bucket/tabla/API y demás formas usadas por CUR.

Persistir snapshots fechados al desplegar y al ejecutar el reporte. Cada entrada
debe tener `environment`, `stack`, `resourceType`, `logicalId`, `physicalId`,
`arn`, alias, `validFrom` y `validTo`. Esto evita perder el costo de un recurso
reemplazado que ya no aparece en el estado actual de CloudFormation.

### 9.3 Reglas de unión

Para cada fila de CUR:

1. **Directo:** `line_item_resource_id` coincide con un alias de un recurso del
   inventario vigente en esa fecha.
2. **Por principal:** para Bedrock,
   `line_item_iam_principal` corresponde a un rol del stack.
3. **Ledger del proyecto:** el costo por documento proviene de `CostTable`; se
   muestra separado o se usa para explicar el cargo AWS, evitando doble conteo.
4. **Estimado:** existe una métrica reproducible por recurso, pero CUR no ofrece
   identificación suficiente. Debe mostrarse como estimación, nunca dentro del
   total exacto.
5. **No atribuible:** transferencia, API requests u otros line items sin recurso
   o principal comprobable. Se excluyen del total exacto y se muestran con monto
   y motivo.

No debe existir un fallback que asigne a este proyecto todo el costo de Lambda,
DynamoDB, S3, API Gateway, KMS, Bedrock o cualquier otro servicio de la cuenta.

El reporte debe mostrar:

- Costo AWS directamente atribuible.
- Costo atribuido por principal Bedrock.
- Estimaciones separadas.
- Monto no atribuible.
- Porcentaje de cobertura de atribución.
- Desglose prod/dev y vista combinada.
- Comparación contra el ledger por documento sin sumar ambos como si fueran
  gastos distintos.

### 9.4 Interfaces internas propuestas

Mantener `collect_infrastructure_costs(...)` como punto de entrada y ampliar su
resultado con campos compatibles hacia atrás:

```python
{
    "attribution": {
        "exactUsd": 0.0,
        "principalUsd": 0.0,
        "estimatedUsd": 0.0,
        "unattributedUsd": 0.0,
        "coveragePercent": 0.0,
        "rows": [],
    },
    "inventory": {
        "capturedAt": "...",
        "resources": [],
    },
}
```

Cada fila atribuida debe conservar periodo, servicio, usage type, operación,
resource ID, principal IAM, costo, ambiente, regla de coincidencia y nivel de
certeza. Los formatos Markdown/HTML/XLSX de `pnpm reporte` deben consumir la
misma estructura.

No se requiere cambiar la REST API pública del IDP para esta tarea.

## 10. Validación y criterios de aceptación

### 10.1 Datos

- Todos los scans paginados terminan y los conteos de items quedan en el manifiesto.
- El manifiesto y los archivos del paquete pasan SHA-256.
- Esquema, PK/SK, GSIs, TTL, PITR y cifrado coinciden con lo esperado.
- Comparar conteo y hash canónico de claves/items entre origen y destino.
- Comparar inventario, tamaño y checksum de objetos S3.
- Confirmar que todas las imágenes `references/` existen.
- Confirmar que `refs:reindex` reproduce el conteo del índice vectorial.
- Verificar catálogos activos y fallback de modelos.

### 10.2 Aplicación

- Una API key de prod y una de dev autentican con los mismos permisos.
- Alta → presigned PUT → procesamiento → consulta termina correctamente.
- Consulta de un documento histórico devuelve su resultado y archivo original.
- Búsqueda por unique key funciona.
- Registro/consulta de referencia funciona.
- El reporte encuentra costos y estadísticas históricas restauradas.

Las pruebas reales de extracción consumen Bedrock/Textract y deben ejecutarse
deliberadamente. Primero correr las validaciones gratuitas del repositorio y
después un smoke test pagado mínimo.

### 10.3 Costos

Agregar pruebas unitarias para:

- Separación estricta prod/dev.
- Recurso físico coincidente y no coincidente.
- Recurso reemplazado dentro de su ventana `validFrom`/`validTo`.
- Bedrock atribuible por rol IAM.
- Line item sin resource ID.
- Cargo de otro proyecto que usa el mismo servicio.
- Ausencia total de tags `CostStack`.
- Créditos/reembolsos separados.
- Falla de CUR/Athena sin inventar un total.
- Fusión multiambiente sin duplicar costos.

Comparar una muestra del resultado con una consulta Athena manual. El total
exacto del reporte debe ser la suma de filas respaldadas por resource ID o
principal; ninguna fila estimada/no atribuible puede entrar silenciosamente.

### 10.4 Validación del repositorio

Como mínimo:

```bash
pnpm build
pnpm test:unit
```

Ejecutar además las pruebas específicas de reportes e infraestructura. No correr
deploys ni suites pagadas como parte de una validación automática sin aprobación.

## 11. Rollback y cierre

La cuenta origen se conserva intacta hasta que:

- La aplicación destino complete los smoke tests.
- Se haya operado durante el periodo acordado sin pérdidas.
- Los reportes muestren datos y costos esperados.
- El responsable funcional confirme que documentos, catálogos, referencias y
  estadísticas están completos.

Después del corte:

- Revocar la política cross-account temporal y el rol de migración.
- Rotar credenciales temporales.
- Conservar el respaldo local cifrado según la política de retención.
- No destruir la cuenta origen hasta aprobar formalmente el cierre.
- Registrar fecha, responsables, hashes del respaldo y evidencia de validación.

## 12. Riesgos y límites conocidos

- Sin una cuenta exclusiva o tags no existe atribución universalmente exacta
  para line items que AWS publica sin `resource_id` ni principal. El reporte
  debe reconocer ese límite.
- Activar CUR 2.0 no reconstruye retroactivamente el detalle que nunca fue
  generado.
- El TTL ya pudo eliminar estadísticas operativas antiguas.
- El modo recomendado exige que las tablas/buckets del stack destino estén
  vacíos y sin notificaciones; el modo aislado exige que las tablas no existan.
- API key IDs cambian entre cuentas aunque se preserve su valor.
- KMS keys no se transfieren entre cuentas.
- CloudFormation no incluye automáticamente recursos importados por nombre en
  `ListStackResources`.
- La producción desplegada contiene infraestructura histórica distinta del CDK
  actual; desplegar sin reconciliar puede reemplazar o eliminar recursos.

## 13. Referencias oficiales

- [Migrar una tabla DynamoDB entre cuentas](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-migrating-table-between-accounts.html)
- [Exportar DynamoDB a S3 e importar en otra cuenta](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-migrating-table-between-accounts-s3.html)
- [Solicitar un export de DynamoDB](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/S3DataExport_Requesting.html)
- [API ExportTableToPointInTime](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_ExportTableToPointInTime.html)
- [Scan y ausencia de aislamiento de snapshot](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Scan.html)
- [BatchWriteItem para restauración directa](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_BatchWriteItem.html)
- [CUR 2.0 y configuraciones INCLUDE_RESOURCES/INCLUDE_IAM_PRINCIPAL_DATA](https://docs.aws.amazon.com/cur/latest/userguide/table-dictionary-cur2.html)
- [Columnas line item de CUR 2.0](https://docs.aws.amazon.com/cur/latest/userguide/table-dictionary-cur2-line-item.html)
- [Crear un AWS Data Export estándar](https://docs.aws.amazon.com/cur/latest/userguide/dataexports-create-standard.html)
- [Atribución de Bedrock por principal IAM](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/iam-principal-cost-allocation.html)
- [ListStackResources](https://docs.aws.amazon.com/cli/latest/reference/cloudformation/list-stack-resources.html)

## 14. Próximo paso de la sesión que retome el trabajo

1. Trabajar desde `/home/luis-pc/Desktop/documents-validator` y leer
   `AGENTS.md` completo.
2. Confirmar branch y cambios locales antes de editar.
3. Repetir el inventario AWS de solo lectura para detectar cambios desde este
   análisis.
4. Inspeccionar `documentacion/COSTOS.md`,
   `documentacion/AMBIENTES_Y_SEGURIDAD.md`,
   `documentacion/ARQUITECTURA.md` y las pruebas de
   `scripts/infrastructure_costs.py`.
5. Implementar primero las protecciones CDK y sus pruebas.
6. Preparar y probar los comandos interactivos de inventario, respaldo y restauración.
7. Solicitar aprobación explícita antes de cualquier deploy, activación de
   backup, export, import, modificación de API Gateway o corte de tráfico.
