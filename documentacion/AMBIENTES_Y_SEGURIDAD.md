# Ambientes, dominio propio y seguridad

Dos ambientes, un solo código. `prod` va endurecido (dominio propio + WAF +
API keys); `dev` queda abierto en el URL random de Amazon para probar fácil.

| | dev | prod |
|---|---|---|
| Stack | `DocumentValidationStack-Dev` | `DocumentValidationStack` |
| URL | `execute-api` random de Amazon | `https://idp.alsuper.com` (o random si no configuras dominio) |
| Auth | ninguna por default; **API key** opcional vía `DEV_API_KEY_REQUIRED=true` + redeploy | **API key** (`x-api-key`) en todos los endpoints, siempre |
| Rate limit / anti-DDoS | sin WAF; throttle de stage **compartido por todos** (10 req/s, burst 20 en escrituras; 40/80 por ruta GET) + usage plan por app cuando hay keys | **WAF** (rate-based por IP, escrituras y GET por separado, + managed rules) + usage plan por app |
| Documentos/día por app | `MaxDocsPerDay` (si la app lo tiene) | `MaxDocsPerDay` (si la app lo tiene) |
| TLS | sí (execute-api) | sí (cert propio, TLS 1.2+) |
| Recursos globales | sufijo `-dev` (`idp-broker-dev`); **sin CMK propia** | nombres base (`idp-broker`, alias KMS `…/pipeline`) |
| Catálogo (DynamoDB) | `document-validation-catalog-dev` | `document-validation-catalog` |

Cada ambiente es un **stack aislado y completo** (sus propias Lambdas, Step
Functions, S3, tabla DynamoDB de tracking y EventBus), **incluida la tabla de
catálogo**: dev usa `document-validation-catalog-dev` y prod
`document-validation-catalog` (ambas importadas por nombre desde
`CATALOG_TABLE`, no las crea el stack). Por eso dev es un espejo funcional para
probar sin tocar prod: editar el catálogo en dev (`pnpm sync`) no puede afectar
a producción, y las Lambdas de dev ni siquiera tienen permiso IAM sobre la
tabla de prod. Lo que sí sigue compartido es
`document-validation-fallback-models` (lista de modelos de fallback, sin datos
por ambiente).

> **Estado actual:** dev **ya está desplegado** — su URL sale del output
> `ApiEndpoint` del stack `DocumentValidationStack-Dev`
> (`aws cloudformation describe-stacks --stack-name DocumentValidationStack-Dev
> --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text`).
> Prod sigue en su versión pública actual hasta que corras `deploy:prod`.

## Desplegar

```bash
pnpm run deploy         # = DEV por default (seguro, nunca toca prod)
pnpm run deploy:dev     # DocumentValidationStack-Dev (abierto, DNS random)
pnpm run deploy:prod    # DocumentValidationStack (dominio + WAF + API keys) — explícito
pnpm run diff:dev / diff:prod
```

### Atribución de costos por stack

Los dos stacks llevan siempre la etiqueta de asignación `CostStack`, declarada
en `bin/app.ts` como metadata del stack y propagada por CloudFormation a los
recursos que soportan etiquetas:

| Stack | Etiqueta |
|---|---|
| `DocumentValidationStack-Dev` | `CostStack=DocumentValidationStack-Dev` |
| `DocumentValidationStack` | `CostStack=DocumentValidationStack` |

Agregarla o actualizarla no cambia nombres físicos, referencias, rutas ni
configuración funcional. Un despliegue normal de cada ambiente basta para
aplicarla a los recursos administrados por sus respectivos stacks.

`CDKToolkit` es el bootstrap compartido y no forma parte del árbol de ninguno
de los dos stacks. Los comandos de despliegue lo crean o actualizan de manera
idempotente con `CostStack=CDKToolkit`; el comando explícito queda disponible
para mantenimiento:

```bash
pnpm run bootstrap:cost-tags
```

Las tablas `document-validation-catalog-dev` y
`document-validation-catalog` se **importan por nombre**, por lo que
CloudFormation no administra su ciclo de vida. La preparación integrada en
cada deploy las crea si faltan y asegura el `CostStack` de su ambiente. Los
comandos explícitos son idempotentes y quedan disponibles para diagnóstico:

```bash
pnpm run tag:costs:dev
pnpm run tag:costs:prod
```

Si la tabla ya existe, el comando sólo llama `DescribeTable`, `TagResource` y
`ListTagsOfResource`: no modifica datos ni esquema. La tabla compartida
`document-validation-fallback-models`, cuando exista, no debe atribuirse a dev
ni a prod; se reporta como infraestructura compartida.

Finalmente, la clave `CostStack` debe activarse en **Billing and Cost
Management → Cost allocation tags** desde la cuenta pagadora. Hacerlo desde
una cuenta vinculada no está permitido por AWS.

La config por ambiente vive en `.env.dev` y `.env.production` (ambos gitignored;
plantilla en `.env.example`). El ambiente se elige con `-c env=dev|prod` (lo
hacen los scripts). Cada `.env.<env>` puede sobreescribir, entre otros, el
**modelo Bedrock** (`BEDROCK_MODEL_ID`) — p. ej. probar otro modelo en dev sin
tocar prod. Cambiar el modelo requiere **redeploy** de ese ambiente (la var se
hornea en la Lambda). Los catálogos generados (`config/catalogs.json`,
`query-lambda/document-types.json`) están gitignored: se regeneran con `pnpm build`.

### Cifrado por ambiente y nombres únicos

Dev conserva cifrado en reposo sin crear llaves facturables del proyecto:
SSE-S3 en el bucket, SSE-SQS en las colas, llave propia del servicio en
DynamoDB/Step Functions y cifrado default en Lambda, logs, EventBridge y SNS.
El synth se protege con `pnpm test:encryption`, que exige **0** recursos
`AWS::KMS::Key` en dev.

Prod utiliza un único CMK con alias `document-validation/pipeline`, compartido
por los recursos que requieren control de llave, incluido S3. El mismo test
exige exactamente **1** llave en el template de prod. El nombre del EventBus sí
es único por cuenta, por lo que dev usa `idp-broker-dev` y prod `idp-broker`.

La notificación `S3 raw/ → SQS` mantiene su queue policy restringida al ARN del
bucket. El permiso KMS para el principal de servicio S3 se declara sin volver a
referenciar ese ARN: como bucket y cola comparten el mismo CMK, copiar el
`SourceArn` a la key policy crearía el ciclo CloudFormation `bucket → key →
bucket`. `pnpm test:encryption` valida también que los templates de ambos
ambientes no contengan ciclos de dependencias entre recursos.

## Dominio propio `idp.alsuper.com` (solo prod)

### 1. Importar tu certificado a ACM (una vez)
Tú provees el cert por archivos `.cert` + `.key` (+ chain). Se importan a ACM
(es una llamada de API, no CloudFormation):

```bash
node scripts/import-cert.mjs --cert idp.cert --key idp.key --chain chain.pem
# imprime el ARN → pégalo en .env.production:
#   DOMAIN_NAME=idp.alsuper.com
#   CERT_ARN=arn:aws:acm:us-east-1:...:certificate/....
```
> Los `.cert`/`.key` son secretos: están en `.gitignore`, nunca se commitean.

### 2. Desplegar prod
```bash
pnpm run deploy:prod
```
El output **`CustomDomainTarget`** trae el destino regional del API Gateway.

### 3. Apuntar tu DNS (externo, no Route53)
En el DNS de `alsuper.com` agrega un **CNAME**:
```
idp.alsuper.com  CNAME  <CustomDomainTarget del output>
```
Listo: `https://idp.alsuper.com` resuelve con tu cert.

### Renovar el certificado SIN downtime
Un cert importado no auto-renueva. La clave: **re-importar al MISMO ARN** — API
Gateway toma el cert nuevo al instante, sin deploy y sin tocar el DNS:

```bash
node scripts/import-cert.mjs --renew --arn "$CERT_ARN" \
  --cert nuevo.cert --key nuevo.key --chain chain.pem
```
`CERT_ARN` no cambia; el `CustomDomainTarget` no cambia; el CNAME sigue igual.
(ACM publica la métrica `DaysToExpiry` en CloudWatch si quieres una alarma.)

## Seguridad (prod, y dev si activas `DEV_API_KEY_REQUIRED`)

### API keys — protege y evita consumo por 401/403/404
Todos los endpoints exigen `x-api-key`. Un request sin key válida recibe **403
nativo de API Gateway sin invocar la Lambda** → **no hay consumo de backend** por
requests no autorizados o rutas inexistentes (404). Solo requests válidos (200) o
errores reales del backend consumen Lambda/Bedrock.

Hay dos mecanismos de API key, que **coexisten sin conflicto** (son recursos
AWS físicos distintos):

1. **`idp-default`** — la key única gestionada por CDK (`src/document-validation-stack.ts`,
   bloque `if (isProd)`), creada automáticamente al desplegar prod. Su id sale
   en el output `ApiKeyId`:
   ```bash
   aws apigateway get-api-key --api-key <ApiKeyId> --include-value \
     --region us-east-1 --query value --output text
   ```
2. **Apps registradas** — una API key + usage plan **por sistema consumidor**
   (Promocontrol, CEDIS/ERP, Portal…), con su propio rate limit/cuota, creadas
   **sin redeploy** contra el stack ya desplegado, vía `pnpm apps:*`
   (`scripts/manage-apps.mjs`). Es el mecanismo recomendado para cualquier
   app nueva — `idp-default` existe solo por compatibilidad con quien ya la
   usaba antes de que este mecanismo existiera.

Usar cualquiera de las dos keys es igual:
```bash
curl -H "x-api-key: <VALOR>" https://idp.alsuper.com/documents/...
# El script lee API_KEY (preferida) o API_KEY_APP de .env.production:
pnpm enviar archivo.pdf PEDIMENTO_ERP --env prod
```

### Límites por app: qué cuenta y qué no

Cada app registrada tiene **cuatro límites independientes**. Solo uno de ellos
("documentos por día") se entiende en documentos; los demás cuentan requests:

| Límite | Dónde vive | Qué cuenta | Al pasarse | Default de `register` |
|---|---|---|---|---|
| **Throttle de escritura** (req/s + burst) | override por método del usage plan | `POST /documents`, `POST /upload`, `POST /documents/{id}/reprocess`, `PATCH /documents/{id}/metadata`, y de la API de referencias: `POST /references/{scope}/{key}`, `POST …/{kind}/{refId}/confirm` y `DELETE …/{kind}/{refId}` | 429 `{"message":"Too Many Requests"}` | 20 / 40 |
| **Throttle de lectura** (req/s + burst) | throttle del usage plan | **todos los GET**: polling de `/documents/{id}`, `/file`, búsquedas, `/document-types` | 429 `{"message":"Too Many Requests"}` | 4× escritura (80 / 160) |
| **Documentos por día** (`MaxDocsPerDay`) | fila de la app en `document-validation-apps[-dev]`; el contador en `document-validation-app-usage[-dev]` | solo `POST /documents`, `POST /upload` y reprocess **que pasaron validación** — los GET nunca | 429 `{"error":"DAILY_DOCUMENT_LIMIT", ...}` con `Retry-After` hasta la medianoche UTC | sin tope |
| **Techo de requests por día** | cuota del usage plan | **todo**, incluido el polling | 429 `{"message":"Limit Exceeded"}` hasta la medianoche UTC | 5000 |

Por qué está repartido así:

- **La cuota de API Gateway no distingue métodos.** Cuenta cada request de la
  key y no se puede excluir ninguno, así que un cliente que hace polling cada
  5 s gasta ~50 requests por documento (p95 de procesamiento medido en dev:
  ~238 s). Por eso "documentos por día" no puede ser la cuota: lo cuenta la
  Lambda (`daily-doc-cap.mjs`, copia idéntica en upload-lambda y query-lambda)
  con un `UpdateItem` condicional atómico por (key, día UTC), y la cuota queda
  como techo anti-abuso amplio (`recommendLimits` la calcula como
  docs/día × 50, mín. 1000).
- **Lectura = throttle del plan, escritura = override por método.** AWS no
  documenta si un override por método de un usage plan puede ser *mayor* que
  el throttle del plan; *menor* sí funciona siempre. Con este reparto el
  resultado es correcto en cualquier caso. La lista de rutas de escritura es
  `WRITE_METHODS` en `scripts/_app-limits.mjs`; un test
  (`tests/unit/node/app-limits.test.mjs`) falla si el stack gana una ruta
  no-GET que no esté ahí.
- **Fail-open.** Si el contador de DynamoDB falla por cualquier razón que no
  sea "tope alcanzado", el documento pasa (con log): un problema del contador
  nunca tumba subidas, y el techo de la cuota sigue de respaldo. Sin
  `MaxDocsPerDay`, sin key (dev abierto) o con una key no registrada no hay
  tope de documentos.
- **Las referencias no son documentos.** Las rutas `/references` no tocan el
  contador de `MaxDocsPerDay`: registrar una firma no dispara Textract ni un
  modelo generativo (una confirmación cuesta un embedding de $0.00006). Sí
  cuentan para el throttle de escritura (`POST`/`DELETE`, en `WRITE_METHODS`),
  el de lectura (sus dos `GET`, también en `DEV_READ_METHOD_PATHS` del stack) y
  el techo de requests.
- **Día UTC** en los dos límites diarios (la cuota de API Gateway también se
  reinicia a las 00:00 UTC = 18:00 de Ciudad de México).
- **Los throttles son aproximados; el tope de documentos es exacto.** AWS
  aplica rate/burst "best-effort", con buckets distribuidos entre nodos.
  Medido en dev el 2026-09-21 con una app de escritura 1/s (burst 1) y GET
  10/s (burst 20): a 8 req/s sostenidos, los POST recibieron 22 de 64 `429`
  (pasaron ~5/s) y los GET 0 de 64; a 30 GET/s pasaron ~21/s. Ráfagas cortas
  de 10 POST o 40 GET concurrentes pasaron completas. Sirven para frenar un
  loop o un abuso sostenido, no como contador preciso — lo que protege el
  costo con exactitud es `MaxDocsPerDay` (atómico en DynamoDB: el 4º
  documento con tope 3 siempre recibe `DAILY_DOCUMENT_LIMIT`).

Además de los límites de la app, cada ambiente tiene uno propio que no depende
de la key: en **dev**, el throttle del stage (10 req/s, burst 20 para
escrituras; 40/80 por cada ruta GET), **compartido por todas las apps**; en
**prod**, el WAF por IP (2000 escrituras y 10000 GET cada 5 min), que bloquea
con **403** `{"message":"Forbidden"}`, no con 429.

**Planes anteriores a esta separación** tienen un solo throttle para todo (GET
y escrituras comparten el mismo bucket). `pnpm apps:usage` los marca como
"plan antiguo"; fijar cualquier límite con `apps:reconfigure` (sección 5) los
separa conservando los valores actuales de lo que no se pidió.

**Ver los límites reales y el consumo** — solo lectura, no toca nada:
```bash
pnpm apps:usage -- --env dev                 # todas las apps ACTIVE, últimos 7 días
pnpm apps:usage -- --env prod --name ERP --days 30
```
Por app muestra los límites **leídos del usage plan real** (no la copia de
DynamoDB) y, por día UTC, requests usadas/restantes de la cuota (`get-usage`)
y documentos contados contra `MaxDocsPerDay`. Las requests rechazadas con 429
o 403 no aparecen: nunca llegaron a consumir cuota. El conteo de requests
(`get-usage`) **no es en tiempo real** — lo de hoy puede tardar en aparecer;
el de documentos (DynamoDB) sí es inmediato.

### Ciclo de vida de una app registrada

Todos los comandos aceptan `--env dev|prod` (default `dev`), `--stack <NOMBRE>`
explícito si el nombre del stack no es el estándar, y `--region` (default
`AWS_REGION` o `us-east-1`). Ninguno necesita `cdk deploy`: hablan directo con
API Gateway y con la tabla `document-validation-apps[-dev]` ya desplegada.

**Todos los que mutan algo** (`register`, `rotate`, `reconfigure`, `disable`,
`enable`, `revoke`, `adopt` — `list` y `usage` son de solo lectura) son **interactivos**
si corren en una terminal real y falta algún dato, y **siempre** terminan con
una confirmación `(S/n)` antes de tocar AWS — incluso si ya diste todos los
flags. `--yes`, o correr sin TTY (scripts/CI), se salta todo eso (falla si
falta algo requerido) para no bloquear automatización.

#### 1. Generar una API key nueva (`register`)

**Modo interactivo** (recomendado si no sabes de memoria qué rate limit poner)
— corre sin flags, o con solo algunos, en una terminal real:
```bash
pnpm apps:register
```
Pregunta **solo lo que falte** (nombre, ambiente, y si falta cualquier
límite, el **volumen de documentos por día** esperado para esa app) y
recomienda valores por tramo:

| Volumen/día | escritura (req/s / burst) | GET (req/s / burst) | docs/día | techo requests/día |
|---|---|---|---|---|
| ≤ 50 | 5 / 10 | 20 / 40 | volumen×1.5 (mín. 20) | docs/día × 50 (mín. 1000) |
| 51–500 | 10 / 20 | 40 / 80 | ídem | ídem |
| 501–2000 | 20 / 40 | 80 / 160 | ídem | ídem |
| 2001–10000 | 40 / 80 | 160 / 320 | ídem | ídem |
| > 10000 | 80 / 160 | 320 / 640 | ídem | ídem |

(`scripts/_app-limits.mjs::VOLUME_TIERS`/`recommendLimits` — rate/burst por
tramo porque en este dominio, subida de documentos y no llamadas de alta
frecuencia, protegen contra un loop/burst anómalo más que limitar tráfico
legítimo; los GET se recomiendan a 4× la escritura porque solo cuestan
Lambda + DynamoDB. Documentos/día sí se calcula fino sobre el número que des,
con 50% de margen para reintentos y reprocesos; el techo de requests deja
espacio para el polling de cada documento — ver "Límites por app" arriba).
Si cambias la escritura, la recomendación de GET se recalcula a 4× lo que
escribiste. Cada pregunta muestra el valor recomendado
entre corchetes — Enter lo acepta, o escribes el tuyo (como en el segundo
ejemplo arriba: volumen 50000 recomienda rate=80, pero se aceptó `100`
a mano). Al final pide confirmación con un resumen antes de tocar AWS.
Cualquier flag que ya hayas pasado **no se vuelve a preguntar** — si diste
los seis límites, tampoco pregunta el volumen. `--yes`, o correr sin TTY
(pipes, CI), desactiva las preguntas por completo y usa flags/defaults tal
cual (falla si falta `--name`) — igual que el comportamiento antes de que
existiera el modo interactivo.

**Modo directo**, para scripting o cuando ya sabes los números:
```bash
pnpm apps:register -- --name CEDIS --env prod \
  --rate-limit 10 --burst-limit 20 \
  --read-rate-limit 40 --read-burst-limit 80 \
  --docs-per-day 300 --quota-per-day 15000
```
Todos los límites son opcionales incluso aquí: escritura `20`/`40`, GET 4×
la escritura, techo `5000` y **sin** tope de documentos (lo mismo que hacía
antes de existir el tope, para no cambiar scripts existentes), sin pasar por
la recomendación por volumen.

En ambos modos: el comando imprime el valor de la key **una sola vez** en
pantalla — cópialo antes de cerrar la terminal; si lo pierdes, se reobtiene
con el mismo `aws apigateway get-api-key --include-value` que ya usa
`idp-default` (ver arriba), nunca hace falta regenerarla solo por eso. Falla
explícitamente si ya existe una app `ACTIVE` con ese nombre — para
reemplazarla usa `rotate` (abajo), no `register` de nuevo (evita quedar con
dos keys `ACTIVE` del mismo nombre, que ni `revoke` ni `rotate` podrían
desambiguar solas). `--force` sí crea una key adicional a propósito si de
verdad quieres dos credenciales activas para la misma app en paralelo.

#### 2. Deshabilitar / re-habilitar una API key (`disable` / `enable`, o `revoke`)
```bash
pnpm apps:disable -- --name CEDIS --env prod
pnpm apps:enable -- --name CEDIS --env prod    # revertir: misma key, sin crear nada
```
`disable` deja la key **inhabilitada en API Gateway** (`update-api-key` con
`enabled=false`) y marca la fila en DynamoDB como `Status=REVOKED` con
`RevokedAt`. **No borra nada**: `enable` la reactiva (misma key, `Status=ACTIVE`
de nuevo) sin crear ni borrar ningún recurso. `pnpm apps:revoke -- --name X`
(sin `--delete`) hace exactamente lo mismo que `disable` — son equivalentes,
`disable`/`enable` solo tienen nombres más directos para ese par de acciones.

> **OJO — no es instantáneo.** Un `enabled=false` puede tardar de unos
> segundos a varios minutos en propagarse a los nodos de borde de API
> Gateway (mismo tipo de retraso que los cambios de usage plan, sección 5).
> Un request enviado justo después de `disable` puede pasar igual una vez
> más — no es que el comando haya fallado. Verifícalo sin gastar en
> Bedrock/Textract pegándole a un endpoint de lectura:
> ```bash
> curl -s -o /dev/null -w "%{http_code}\n" -H "x-api-key: <VALOR>" "$API_ENDPOINT/documents?limit=1"
> ```
> `403` confirma que ya se propagó; si ves `200`, espera un poco y reintenta.

#### 3. Borrar una API key (`revoke --delete`)
```bash
pnpm apps:revoke -- --name CEDIS --env prod --delete
```
Primero hace lo mismo que el punto 2, y además **elimina los recursos AWS**
(`delete-usage-plan-key` → `delete-usage-plan` → `delete-api-key`) —
**irreversible**, el `ApiKeyId` deja de existir. La fila en DynamoDB **se
conserva** como `Status=REVOKED` a propósito: documentos ya procesados con
esa key guardaron el nombre de la app directamente en su `appOrigin` (no el
`ApiKeyId`), así que borrar la fila no les haría nada — se conserva solo como
historial de auditoría de qué apps existieron.

#### 4. Regenerar/rotar una API key para la misma app (`rotate`)
```bash
pnpm apps:rotate -- --name CEDIS --env prod
```
El caso de uso típico: la key se filtró, o toca rotarla por política. Un solo
comando: crea una key + usage plan **nuevos** con los mismos límites que
tenía la app (a menos que pases cualquiera de los flags de límites para
cambiarlos también; una app de antes de separar lectura/escritura conserva un
solo throttle para todo), la registra en DynamoDB, y recién
entonces **deshabilita** (no borra) la key vieja — el orden importa: si algo
falla creando la nueva, la vieja se queda activa en vez de dejar a la app sin
ninguna key funcionando. Ambas filas quedan enlazadas (`RotatedFrom` en la
nueva, `RotatedTo` en la vieja) para poder rastrear el historial de una app.
`register --force` **no** es lo mismo que `rotate`: crea una key adicional
sin tocar la vieja ni saber cuál reemplaza a cuál — usa `rotate` salvo que
quieras deliberadamente dos keys activas a la vez. El contador de
documentos/día va por `ApiKeyId`, así que la key nueva empieza el día en cero.

#### 5. Cambiar SOLO los límites, sin tocar la key (`reconfigure`)
```bash
pnpm apps:reconfigure -- --name CEDIS --env prod --read-rate-limit 20 --read-burst-limit 40
pnpm apps:reconfigure -- --name CEDIS --env prod --rate-limit 5 --burst-limit 10
pnpm apps:reconfigure -- --name CEDIS --env prod --docs-per-day 80     # o `none` para quitar el tope
pnpm apps:reconfigure -- --name CEDIS --env prod --quota-per-day 4000
```
Para cuando el volumen de una app cambió y hay que ajustarle el límite, pero
la key en sí sigue siendo válida — a diferencia de `rotate`, **no crea nada
nuevo** y la API key no cambia, así que los consumidores **no necesitan hacer
nada**. Acepta cualquier subconjunto de los flags de límites; lo que omitas se
queda como está:

- Throttle y techo: lee el usage plan **real** (`get-usage-plan`), completa
  lo no pedido con sus valores actuales y aplica un `update-usage-plan` (JSON
  Patch): `/throttle/*` para los GET, `/quota/limit` para el techo, y
  `/apiStages/<api>:<stage>/throttle/documents/POST/rateLimit` (etc.; el resource path va literal, sin escapar como `~1`) para
  cada ruta de escritura — `add` si el override aún no existe, `replace` si
  ya existe. En un **plan antiguo** (un solo throttle para todo), fijar
  cualquier throttle crea los overrides de escritura con el valor de hoy: nada
  se afloja ni se aprieta sin pedirlo.
- `--docs-per-day` es solo un atributo en DynamoDB (`MaxDocsPerDay`), así que
  aplica aunque la app no tenga `UsagePlanId` y surte efecto en el siguiente
  POST, sin propagación.

Falla si pides throttle/techo en una app adoptada sin `UsagePlanId` conocido
(no hay a qué usage plan aplicarle el patch) o si no hay ninguna app `ACTIVE`
con ese nombre. Los cambios de usage plan pueden tardar unos minutos en
propagarse (igual que `disable`); `pnpm apps:usage` muestra lo que el plan
tiene guardado.

#### 6. Restringir qué documentos y qué queries puede usar una app (`register`/`reconfigure`)
```bash
# ERP solo ve lo que ELLA subió (default de cualquier app nueva) — no hace falta nada:
pnpm apps:register -- --name ERP --env prod

# Portal admin: ve todo, sin restricción
pnpm apps:reconfigure -- --name Portal --env prod --access-scope all

# CEDIS: además de "solo lo suyo", nunca puede tocar otro tipo que no sea el suyo
pnpm apps:reconfigure -- --name CEDIS --env prod --allowed-doctypes FACTURA_HONORARIOS_XML

# Una app de soporte que sí necesita el buscador libre (caro, ver abajo)
pnpm apps:reconfigure -- --name Soporte --env prod --access-scope all --allow-field-search

# ERP puede mantener sus estados/marcas externos después del procesamiento
pnpm apps:reconfigure -- --name ERP --env prod --allow-metadata-write

# Portal de proveedores puede registrar/consultar firmas, sellos y logos válidos
pnpm apps:reconfigure -- --name Portal --env prod --allow-reference-write
pnpm apps:reconfigure -- --name Portal --env prod --disallow-reference-write   # quitarlo
```
Seis controles, todos atributos en DynamoDB (no en API Gateway — no requieren
redeploy ni tocan la key). Los cuatro primeros los aplica `query-lambda` en cada
`GET /documents/{id}`, `GET /documents/{id}/file`, `GET /documents?...` y
`PATCH /documents/{id}/metadata`, además de
`POST /documents/{id}/reprocess`; el quinto lo aplica la References Lambda
(`references_api.py`) en todas las rutas `/references`:

| Flag | Default (app nueva) | Qué controla |
|---|---|---|
| `--access-scope own\|all` | `own` | `own`: solo documentos cuyo `tracking.appOrigin` sea la app misma; mientras aún no existe el envelope se usa el `JobOwner` reservado por Upload. También acota qué original puede reprocesar. `all`: sin restricción. |
| `--allowed-doctypes A,B` | (ninguno = sin restricción) | Restringe incluso a una app `all` al consultar, descargar, reprocesar o editar metadata de esos tipos. No bloquea el upload. `none` en `reconfigure` quita la restricción. |
| `--allow-field-search` | `false` | Si puede usar `GET /documents?field=&value=` (el Scan-based, el más caro y abusable — ver "Cómo funciona en el backend" abajo). Sin esto, solo `docType=`/`{id}` — y `system=`/`meta.<clave>=`, que **no** están detrás de este permiso: leen una sola clave conocida del envelope en vez de recorrerlo entero, y no amplían lo que la app ya puede ver (con `AccessScope: own`, `system=` solo puede acotar más). |
| `--allow-metadata-write` | `false` | Si puede usar `PATCH /documents/{id}/metadata` para estados transitorios/marcas propios. No permite cambiar `validation.status`; sigue limitado por scope y tipos. |
| `--allow-reference-write` / `--disallow-reference-write` | `false` | Si puede usar la API de referencias de autenticidad (`/references/...`): registrar, listar, ver y dar de baja las firmas, sellos y logos válidos de una entidad. Cubre **todas** las rutas, también los `GET` — listar referencias expone firmas de personas. Atributo `AllowReferenceWrite`. Es independiente de `AccessScope`/`AllowedDocTypes`: las referencias son de la entidad (`scope` + `key`), no de un documento ni de un tipo. `rotate` lo conserva. |
| `--allow-ai-response-format-override` / `--disallow-ai-response-format-override` | `false` | Permite usar `aiResponseFormat=compact|legacy` en upload o reproceso de producción. Controla costo/compatibilidad del protocolo interno, no acceso a documentos. Atributo `AllowAiResponseFormatOverride`; `rotate` lo conserva. En dev el stack permite el override para benchmarks. |

**Por qué 404, no 403, en un documento fuera de alcance**: `GET /documents/{id}`
y `.../file` —y la búsqueda del original para `.../reprocess`— devuelven `404 NOT_FOUND` cuando el documento existe pero la app
no puede verlo — igual que si no existiera. No confirmar la existencia evita
que una app pueda enumerar/inferir qué IDs son válidos aunque no pueda leerlos.
`GET /documents?...` (listar/buscar) simplemente omite esos resultados de
`matches`, sin marcar error. Un `docType=` o `field=` explícitamente prohibido
sí devuelve `403 FORBIDDEN` con el motivo — ahí la app SÍ sabe qué pidió, así
que ocultarlo no suma nada.

La fila ligera creada por `POST /documents` guarda `JobOwner` y, cuando se
declaró, `RequestedDocumentType`. Por eso el `202` de un trabajo pendiente
respeta el mismo alcance que el resultado final sin depender todavía de
`tracking.appOrigin`; `404` conserva exactamente la misma protección.

**Apps ya registradas antes de esto** (como `CEDIS`/`idp-default`): quedan en
`own` por default la próxima vez que se lean (no hay redeploy que las toque),
salvo `idp-default` sin adoptar, que se trata como `all` (ver "Cómo funciona
en el backend"). Si una app vieja necesita ver todo, súbela explícito con
`--access-scope all`.

#### 7. Otorgar/quitar UN permiso puntual, sin reescribir todo (`grant` / `deny`)
```bash
# CEDIS ya tenía allowedDocTypes=PEDIMENTO_ERP; agrégale FACTURA_HONORARIOS_XML
# sin tener que saber/retipear lo que ya tenía:
pnpm apps:grant -- --name CEDIS --env prod --doctype FACTURA_HONORARIOS_XML

# Quitarle SOLO ese tipo (el resto de allowedDocTypes queda intacto):
pnpm apps:deny -- --name CEDIS --env prod --doctype FACTURA_HONORARIOS_XML

# Subir/bajar el scope o el acceso al buscador libre, igual de puntual:
pnpm apps:grant -- --name Soporte --env prod --access-scope all --field-search
pnpm apps:deny  -- --name Soporte --env prod --field-search

# Otorgar/quitar solo la edición de metadata:
pnpm apps:grant -- --name ERP --env prod --metadata-write
pnpm apps:deny  -- --name ERP --env prod --metadata-write

# Otorgar/quitar solo la API de referencias de autenticidad (/references):
pnpm apps:grant -- --name Portal --env dev --reference-write
pnpm apps:deny  -- --name Portal --env dev --reference-write
```
`reconfigure --allowed-doctypes A,B` **reemplaza** la lista completa — para
agregar un tipo a una app que ya tenía otros, hay que retipearlos todos.
`grant`/`deny --doctype` en cambio hacen **unión/diferencia** contra lo que la
app ya tenía: `grant` nunca pisa lo existente, `deny` solo quita lo pedido. Si
`deny` deja la lista de tipos permitidos en cero, el atributo se **quita por
completo** (no queda un `AllowedDocTypes` vacío) — DynamoDB no admite un
String Set vacío, y además un `AllowedDocTypes` vacío en el código de
`query-lambda` significaría "sin restricción de tipo", justo lo contrario de
lo que se buscaba al quitar el último tipo permitido.

Antes de aplicar, ambos comandos muestran los permisos **actuales** de la app
(scope, field-search, metadata-write, reference-write, tipos) y qué va a cambiar, con la misma confirmación
`(S/n)` que el resto de comandos. `--field-search` es un flag sin valor cuyo
sentido lo fija el comando: en `grant` siempre habilita, en `deny` siempre
deshabilita. `--metadata-write` y `--reference-write` funcionan igual. `--access-scope own|all` sí toma un valor explícito en ambos
comandos (por si alguna vez necesitas "otorgar" `own` a propósito, ej. una
app que tenía `all` y quieres acotarla sin usar `reconfigure`) — la
diferencia real entre `grant`/`deny` para `--doctype` es la única que importa
para la mayoría de los casos: unión vs. diferencia contra la lista actual.

#### Ver el estado de todas las apps (`list`)
```bash
pnpm apps:list -- --env prod
```
Lista cada app con su `ApiKeyId`/`UsagePlanId`, límites, `Status`, fecha de
alta y (si aplica) de revocación — Scan completo de la tabla, pensado para
uso administrativo ocasional, no para consultarse en un hot path.

#### Migrar `idp-default` al esquema de trazabilidad (`adopt`, opcional)
```bash
pnpm apps:adopt -- --name legacy-idp-default --api-key-id <ApiKeyId de idp-default> --env prod
```
No crea ninguna key ni usage plan nuevos — solo escribe en DynamoDB la fila
que le falta a una key que **ya existe** (típicamente `idp-default`, la que
gestiona CDK), leyendo su usage plan/límites actuales vía `aws apigateway
get-usage-plans --key-id`. Hasta hacerlo, los documentos subidos con
`idp-default` muestran `appOrigin: "unregistered"` (una key válida que API
Gateway aceptó, pero que nunca se registró aquí) — sigue siendo un `appOrigin`
válido, solo menos informativo que un nombre real.

### Cómo funciona en el backend

`register`/`rotate` hacen, en orden, exactamente estas llamadas (todas vía
AWS CLI desde tu máquina, con tus propias credenciales — el script no
despliega ni modifica ningún Lambda):

1. `cloudformation describe-stack-resources` sobre el stack (`DocumentValidationStack[-Dev]`)
   para encontrar el `RestApiId` físico (no hay que saberlo de memoria).
2. `apigateway get-stages` sobre ese `RestApiId` para el nombre del stage
   (`prod`, incluso en dev — es el nombre que CDK usa por default en ambos
   ambientes, no algo que este stack fije explícitamente).
3. `apigateway create-api-key` — crea el objeto ApiKey en API Gateway
   (`--enabled`, con un nombre tipo `idp-<app>-<env>` solo como etiqueta
   legible en la consola; lo único que importa para el sistema es el
   `ApiKeyId` que devuelve).
4. `apigateway get-api-key --include-value` — recupera el **valor real**
   de la key (un secreto de 40 caracteres) para imprimirlo una vez; el
   script nunca lo persiste en ningún lado, ni en DynamoDB ni en disco.
5. `apigateway create-usage-plan` (vía `--cli-input-json`) — crea el usage
   plan asociado al `RestApiId`+stage del paso 1-2, con el throttle de GET a
   nivel plan, el de escritura como override por método en las rutas de
   `WRITE_METHODS` y el techo de requests como cuota diaria.
6. `apigateway create-usage-plan-key` — conecta la ApiKey del paso 3 al
   usage plan del paso 5 (sin esto, la key existiría pero no tendría
   throttle/cuota aplicados — seguiría exigiendo `apiKeyRequired`, pero sin
   límites).
7. `dynamodb put-item` en `document-validation-apps[-dev]` — la única
   escritura que no es contra API Gateway: guarda `ApiKeyId → AppName` (y
   los límites/estado) para que el Upload Lambda pueda resolver el nombre
   y el tope `MaxDocsPerDay` después. Los demás límites en esa fila son una
   copia informativa: los aplica API Gateway, no se leen de ahí.

Del lado de **procesar un documento** (no del script, corre en cada request):

1. API Gateway valida `x-api-key` contra sus propios recursos ApiKey/UsagePlan
   — **nativo, antes de invocar cualquier Lambda**. Si falla, 403, cero costo.
2. Si pasa, el Upload Lambda (`upload-lambda/index.mjs`) lee
   `event.requestContext.identity.apiKeyId` (API Gateway se lo entrega
   automáticamente en el evento cuando el método exige key) y hace un
   `GetItem` **directo** (no Scan) contra `document-validation-apps[-dev]`
   por ese `ApiKeyId` — de ahí la elección de `ApiKeyId` como partition key:
   este es el único lookup que corre en el hot path de cada subida. Si la app
   tiene `MaxDocsPerDay`, después de validar el body suma 1 al contador del
   día (`daily-doc-cap.mjs`) o responde 429 `DAILY_DOCUMENT_LIMIT` sin
   generar la URL prefirmada.
3. El nombre resuelto (`AppName`, o `"unregistered"` si la key es válida pero
   nadie la registró aquí) viaja como metadata del objeto S3 (`app-origin`,
   mismo mecanismo que `model-id` para el override de modelo en dev) — nunca
   como parámetro explícito del body, así que un cliente no puede falsear
   qué app dice ser.
4. El SQS consumer (`patches/default-sqs-consumer-index.py`) lee esa
   metadata con un `head_object` (barato, solo headers) y la reenvía como
   `appOrigin` en el input de Step Functions.
5. El enrichment Lambda la copia tal cual al JSON final, dentro del objeto
   `tracking` de nivel superior (`tracking.appOrigin`, junto a `tracking.modelIds`/
   `tracking.createdAt`) — tanto en el flujo estándar como en el de expediente,
   mismo shape en ambos. Ver `documentacion/api-docs/API_USO.md`.

Si algo de esa cadena falla (permiso, key no registrada, S3 metadata perdida),
el diseño es **fail-open a nivel de trazabilidad**: el documento se procesa
igual, solo con `appOrigin` ausente o `"unregistered"` — nunca se bloquea una
subida por no poder resolver quién la hizo.

**Del lado de leer, reprocesar o editar metadata** (`query-lambda`, cada `GET /documents*`,
`POST /documents/{id}/reprocess` y `PATCH /documents/{id}/metadata`): resuelve el
`ApiKeyId` del caller igual que arriba (mismo `GetItem` contra
`document-validation-apps[-dev]`, mismo hot-path-friendly), y con eso decide
`accessScope`/`allowedDocTypes`/`allowFieldSearch`/`allowMetadataWrite` para esa request. Dos casos
quedan **sin restricción** a propósito (`accessScope: "all"` implícito):
sin `x-api-key` en absoluto (dev con `DEV_API_KEY_REQUIRED` apagado — no hay
identidad que restringir) y una key válida que API Gateway aceptó pero que
**nunca se registró aquí** (`idp-default` antes de `apps:adopt` — el
grandfather case: la key preexistente del sistema no pierde su acceso total
solo por no haberse migrado todavía). Un fallo al resolver los permisos
(DynamoDB caído, etc.) **también falla abierto** — un hipo en esa tabla no
debe convertirse en que nadie pueda leer nada; esto protege segregación de
datos entre apps ya autenticadas, no un secreto. Código: `query-lambda/index.mjs`
→ `resolveCallerApp`/`canSeeDocument`.

Ese fail-open es **solo de lectura**. Una key válida pero no registrada y un
fallo al leer AppsTable reciben `AllowMetadataWrite=false` y tampoco pueden
iniciar reprocesos: una degradación de permisos nunca autoriza una mutación ni
un nuevo gasto de procesamiento. En dev abierto (sin key) sí se permiten PATCH
y reproceso para facilitar pruebas; en prod siempre hay key.

**La API de referencias (`/references`) falla CERRADO, también al leer.** La
References Lambda (`enrichment-lambda/references_api.py` → `resolve_caller`)
resuelve el `ApiKeyId` con el mismo `GetItem` contra
`document-validation-apps[-dev]` y solo deja pasar si la fila tiene
`AllowReferenceWrite = true`. A diferencia de `query-lambda`, aquí no hay
grandfather ni fail-open: una key válida pero no registrada (`idp-default` sin
adoptar), una app sin el permiso y un fallo al leer AppsTable responden todos
`403 FORBIDDEN` — en los `GET` igual que en las escrituras. Registrar una
referencia es declarar "este sello es válido" y listarlas expone firmas, así que
un hipo de la tabla no debe abrir ninguna de las dos cosas. El único caso
abierto es una request **sin** `x-api-key` en un stack que no las exige (dev con
`DEV_API_KEY_REQUIRED` apagado; el stack le pasa `API_KEY_REQUIRED=false` a la
Lambda) — igual que el resto de ese stack. El nombre de la app queda en
`createdBy` / `deactivatedBy` de cada referencia.

**Orden de despliegue de las rutas `/references` (y de cualquier ruta de
escritura nueva):**

1. **Primero `deploy`, después `apps:register`/`apps:reconfigure`.** El throttle
   de escritura es un override por método del usage plan
   (`WRITE_METHODS`), y solo se puede fijar sobre rutas que el stage ya
   publica. Los planes creados **antes** de que existieran las rutas no tienen
   override para ellas: sus `POST`/`DELETE` de `/references` quedan con el
   throttle de **lectura** (más holgado) hasta que corras un
   `pnpm apps:reconfigure` sobre esa app con cualquier flag de throttle.
2. **Si `/references/...` responde `{"message":"Missing Authentication
   Token"}`** con la URL bien escrita, API Gateway no redesplegó el stage: las
   rutas existen en el RestApi pero no en el deployment activo. Se arregla con
   `aws apigateway create-deployment --rest-api-id <RestApiId> --stage-name prod`
   (el stage se llama `prod` también en dev).
3. **`check-pending-tables.mjs` mostrará un cambio en la tabla de huellas**
   (`document-validation-fingerprints[-dev]`): es el TTL `ExpiresAt` que
   limpia las reservas `PENDING` sin confirmar. Es esperado; una referencia
   activa nunca lleva `ExpiresAt`.
4. El permiso en sí no necesita deploy: `pnpm apps:grant -- --name <app> --env
   dev --reference-write` surte efecto en la siguiente request.

### Permisos IAM para ejecutar `manage-apps.mjs` de forma segura

Quien corra `pnpm apps:*` necesita sus propias credenciales AWS (el script no
asume ningún rol ni usa las de ningún Lambda) con, como mínimo:

```jsonc
{
  "Version": "2012-10-17",
  "Statement": [
    {
      // Las acciones de administración de API Gateway (ApiKey/UsagePlan) son
      // un caso especial de IAM: se autorizan contra ARNs de la forma
      // arn:aws:apigateway:<region>::/<recurso> — SIN account id, porque el
      // "recurso" es la propia API de administración de API Gateway, no un
      // objeto de tu cuenta. get-stages necesita el RestApiId real; el resto
      // no distingue una app de otra a nivel de ARN (API Gateway no soporta
      // ABAC por ApiKey individual), así que el mínimo real es a nivel de
      // acción, no de recurso específico.
      "Sid": "GestionarApiKeysYUsagePlans",
      "Effect": "Allow",
      "Action": [
        "apigateway:POST",   // create-api-key / create-usage-plan / create-usage-plan-key
        "apigateway:GET",    // get-api-key / get-stages / get-usage-plans
        "apigateway:PATCH",  // update-api-key (revoke)
        "apigateway:DELETE"  // delete-api-key / delete-usage-plan / delete-usage-plan-key (--delete)
      ],
      "Resource": [
        "arn:aws:apigateway:us-east-1::/apikeys",
        "arn:aws:apigateway:us-east-1::/apikeys/*",
        "arn:aws:apigateway:us-east-1::/usageplans",
        "arn:aws:apigateway:us-east-1::/usageplans/*",
        "arn:aws:apigateway:us-east-1::/restapis/*/stages"
      ]
    },
    {
      // Solo para resolver el RestApiId a partir del nombre del stack — no
      // da acceso a modificar nada del stack.
      "Sid": "ResolverRestApiDelStack",
      "Effect": "Allow",
      "Action": "cloudformation:DescribeStackResources",
      "Resource": "arn:aws:cloudformation:us-east-1:<ACCOUNT_ID>:stack/DocumentValidationStack*/*"
    },
    {
      // Acotado a ESTA tabla específicamente — nunca dynamodb:* genérico.
      "Sid": "TablaDeApps",
      "Effect": "Allow",
      "Action": ["dynamodb:Scan", "dynamodb:PutItem", "dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:us-east-1:<ACCOUNT_ID>:table/document-validation-apps*"
    }
  ]
}
```

Notas sobre esto:
- **No hace falta acceso a Lambda, S3 ni al resto del stack** para administrar
  apps — el script nunca los toca. Si quien administra apps solo debe poder
  hacer eso (y nada de deploys/destroys), esta policy sola ya lo acota sin
  darle de más.
- API Gateway **no tiene una acción IAM separada por operación fina** (no
  existe `apigateway:CreateApiKey` como tal) — las acciones son por verbo
  HTTP (`GET`/`POST`/`PATCH`/`DELETE`) contra la ruta de administración; por
  eso el mínimo privilegio real aquí es "puede administrar ApiKeys/UsagePlans
  de esta cuenta/región", no algo más fino por app individual.
- El valor de la key (el secreto real) solo puede leerse con
  `apigateway:GET` sobre `/apikeys/{id}` con `includeValue=true` — la misma
  acción que listar sin el valor. Quien tenga este permiso puede ver el
  valor de **cualquier** API key de la cuenta en esa región, no solo las que
  administra vía `manage-apps.mjs` (incluida `idp-default`). No hay forma de
  acotar eso más fino a nivel de IAM sin un permission boundary custom.
- Para dev, cambia el ARN del stack/región según corresponda
  (`DocumentValidationStack-Dev*`, `document-validation-apps-dev`).

### Dev con protección activable
Por default dev queda abierto (sin `x-api-key`, sin usage plan). Para exigir
key igual que prod:
```bash
# .env.dev
DEV_API_KEY_REQUIRED=true
```
```bash
pnpm run deploy:dev
pnpm apps:register -- --name test-app --env dev   # sin esto, TODO devuelve 403
```
Es un flag de CDK (`devApiKeyRequired` en `document-validation-stack.ts`), no
un toggle en caliente — cambiarlo siempre requiere `pnpm run deploy:dev`.

**Los scripts de envío detectan esto solos.** `pnpm enviar` (`enviar-documento.sh`),
`pnpm test:e2e:pedimento` (`test-e2e-pedimento.py`) y `pnpm test-e2e`
(`test-e2e.mjs`) — los tres únicos que le pegan a `POST /documents` real — leen
`.env.dev` por su cuenta: si `DEV_API_KEY_REQUIRED=true` y no exportaste
`API_KEY` a mano, usan automáticamente **`API_KEY_APP`** (también de
`.env.dev`, o ya exportado) como `x-api-key`, sin que tengas que repetir
`API_KEY=... ` en cada comando. Flujo típico:
```bash
pnpm apps:register -- --name local-scripts --env dev   # una vez
# pega el valor impreso en .env.dev:
#   API_KEY_APP=<valor>

pnpm enviar pedimento.pdf PEDIMENTO_ERP               # ya no hace falta API_KEY=...
pnpm test:e2e:pedimento -- --file normal_pend.pdf
```
Una `API_KEY` exportada en el shell (o pasada inline:
`API_KEY=<valor> pnpm enviar ...`) **siempre gana** y permite probar con otra
app sin cambiar archivos. En prod, `pnpm enviar` usa después `API_KEY_APP` del
shell y, si tampoco existe, lee `API_KEY` o `API_KEY_APP` de
`.env.production`. Si no encuentra ninguna, se detiene antes del request con
un error claro. En dev conserva el comportamiento anterior: cuando
`DEV_API_KEY_REQUIRED=true`, toma `API_KEY_APP` de `.env.dev`; si falta, avisa
y el request recibirá 403.

### WAF + rate limiting + anti-DDoS
- **Dos reglas rate-based** por IP, cada 5 min: `RateLimitPerIp` para todo lo
  que no es GET (default 2000, `wafRateLimit`) y `RateLimitPerIpReads` solo
  para GET (default 10000, `wafReadRateLimit`). Separadas para que una
  integración que hace polling de muchos documentos desde una sola IP no se
  coma el presupuesto pensado para subidas.
- **Managed rules** de AWS: Common, KnownBadInputs, AmazonIpReputationList.
- **Usage plan por app** (ver "Límites por app"): throttle de escritura y de
  GET por separado + techo de requests/día; tope de documentos/día en Lambda.
- **AWS Shield Standard**: protección L3/L4 automática y gratis (sin config).

Tráfico bloqueado por WAF devuelve 403 **antes** de la integración → tampoco
consume Lambda.

### Configurable
- Por app, sin redeploy: `pnpm apps:register|reconfigure` (ver "Límites por app").
- WAF, en las props del stack (`bin/app.ts`, hoy no se pasan → defaults):
  `wafRateLimit`, `wafReadRateLimit`.
- Solo la key CDK `idp-default`: `apiThrottle` (`rateLimit`/`burstLimit` de
  escritura, default 20/40; `readRateLimit`/`readBurstLimit` de GET, default
  80/160; `quotaPerDay`, default 5000). No afecta a las apps registradas.
- Dev: el throttle de stage (10/20 escrituras, 40/80 por ruta GET) está fijo
  en `src/document-validation-stack.ts` (`DEV_READ_METHOD_PATHS`).

Volumen actual es bajo (~170 pedimentos/mes), así que los defaults son holgados.

## Notas
- Activar API keys en prod es **breaking** para consumidores actuales: deben
  empezar a mandar `x-api-key`. `scripts/enviar-documento.sh` ya soporta `API_KEY`.
- API key = identificación + rate-limit + gate sobre TLS; no es auth fuerte. Si
  luego se requiere, se puede subir a IAM/Lambda authorizer sin rehacer el resto.
- **El aislamiento lógico sí se evalúa en cada request.** `GET`/`PATCH` y
  `POST /documents/{id}/reprocess` resuelven la app de la key y aplican
  `AccessScope`/`AllowedDocTypes`; un documento ajeno responde 404 y no puede
  descargarse ni usarse como fuente de otro proceso. Aun así, una API key no
  identifica a una persona: todos los procesos que compartan la misma key
  comparten el mismo alcance. Usa una key por app; si se necesita autorización
  por usuario/sujeto, sube a authorizer Lambda/IAM. Una URL prefirmada, además,
  es una credencial de acceso en sí
  misma una vez emitida — válida hasta que expira (default 15 min, tope 1h en
  `/file`) para quien sea que la tenga, sin volver a pasar por `x-api-key`.
