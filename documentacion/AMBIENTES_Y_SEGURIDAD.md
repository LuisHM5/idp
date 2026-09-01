# Ambientes, dominio propio y seguridad

Dos ambientes, un solo código. `prod` va endurecido (dominio propio + WAF +
API keys); `dev` queda abierto en el URL random de Amazon para probar fácil.

| | dev | prod |
|---|---|---|
| Stack | `DocumentValidationStack-Dev` | `DocumentValidationStack` |
| URL | `execute-api` random de Amazon | `https://idp.alsuper.com` (o random si no configuras dominio) |
| Auth | ninguna por default; **API key** opcional vía `DEV_API_KEY_REQUIRED=true` + redeploy | **API key** (`x-api-key`) en todos los endpoints, siempre |
| Rate limit / anti-DDoS | no (WAF no aplica en dev); throttle ciego a nivel de stage | **WAF** (rate-based + managed rules) + usage plan por app |
| TLS | sí (execute-api) | sí (cert propio, TLS 1.2+) |
| Recursos globales | sufijo `-dev` (`idp-broker-dev`); **sin CMK propia** | nombres base (`idp-broker`, alias KMS `…/pipeline`) |

Cada ambiente es un **stack aislado y completo** (sus propias Lambdas, Step
Functions, S3, tabla DynamoDB de tracking y EventBus). Comparten solo la
**tabla de catálogo** `document-validation-catalog` (importada por nombre, no se
crea). Por eso dev es un espejo funcional para probar sin tocar prod.

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

### Ciclo de vida de una app registrada

Todos los comandos aceptan `--env dev|prod` (default `dev`), `--stack <NOMBRE>`
explícito si el nombre del stack no es el estándar, y `--region` (default
`AWS_REGION` o `us-east-1`). Ninguno necesita `cdk deploy`: hablan directo con
API Gateway y con la tabla `document-validation-apps[-dev]` ya desplegada.

**Todos los que mutan algo** (`register`, `rotate`, `reconfigure`, `disable`,
`enable`, `revoke`, `adopt` — `list` es de solo lectura) son **interactivos**
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
Pregunta **solo lo que falte** (nombre, ambiente, y si falta cualquiera de
rate-limit/burst-limit/cuota, el **volumen de documentos por día** esperado
para esa app) y recomienda valores por tramo:

| Volumen/día | rate (req/s) | burst | cuota/día sugerida |
|---|---|---|---|
| ≤ 50 | 5 | 10 | ~volumen×1.5 (mín. 100) |
| 51–500 | 10 | 20 | ídem |
| 501–2000 | 20 | 40 | ídem |
| 2001–10000 | 40 | 80 | ídem |
| > 10000 | 80 | 160 | ídem |

(`scripts/manage-apps.mjs::VOLUME_TIERS`/`recommendLimits` — rate/burst por
tramo porque en este dominio, subida de documentos y no llamadas de alta
frecuencia, protegen contra un loop/burst anómalo más que limitar tráfico
legítimo; la cuota diaria sí se calcula fino sobre el número que des, con
30% de margen para reintentos). Cada pregunta muestra el valor recomendado
entre corchetes — Enter lo acepta, o escribes el tuyo (como en el segundo
ejemplo arriba: volumen 50000 recomienda rate=80, pero se aceptó `100`
a mano). Al final pide confirmación con un resumen antes de tocar AWS.
Cualquier flag que ya hayas pasado **no se vuelve a preguntar** — si diste
los tres límites, tampoco pregunta el volumen. `--yes`, o correr sin TTY
(pipes, CI), desactiva las preguntas por completo y usa flags/defaults tal
cual (falla si falta `--name`) — igual que el comportamiento antes de que
existiera el modo interactivo.

**Modo directo**, para scripting o cuando ya sabes los números:
```bash
pnpm apps:register -- --name CEDIS --env prod \
  --rate-limit 30 --burst-limit 60 --quota-per-day 10000
```
`--rate-limit`/`--burst-limit`/`--quota-per-day` son opcionales incluso aquí
(default `20`/`40`/`5000`, igual que `idp-default`, sin pasar por la
recomendación por volumen).

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
comando: crea una key + usage plan **nuevos** con el mismo rate limit/cuota
que tenía la app (a menos que pases `--rate-limit`/`--burst-limit`/
`--quota-per-day` para cambiarlos también), la registra en DynamoDB, y recién
entonces **deshabilita** (no borra) la key vieja — el orden importa: si algo
falla creando la nueva, la vieja se queda activa en vez de dejar a la app sin
ninguna key funcionando. Ambas filas quedan enlazadas (`RotatedFrom` en la
nueva, `RotatedTo` en la vieja) para poder rastrear el historial de una app.
`register --force` **no** es lo mismo que `rotate`: crea una key adicional
sin tocar la vieja ni saber cuál reemplaza a cuál — usa `rotate` salvo que
quieras deliberadamente dos keys activas a la vez.

#### 5. Cambiar SOLO el rate limit/cuota, sin tocar la key (`reconfigure`)
```bash
pnpm apps:reconfigure -- --name CEDIS --env prod --rate-limit 50 --burst-limit 100
```
Para cuando el volumen de una app cambió y hay que ajustarle el límite, pero
la key en sí sigue siendo válida — a diferencia de `rotate`, **no crea nada
nuevo**: hace `update-usage-plan` (JSON Patch por sub-path, solo sobre
`/throttle/rateLimit`, `/throttle/burstLimit` y/o `/quota/limit` — los que
pases) sobre el `UsagePlanId` que ya tiene la app, y actualiza esos mismos
campos en DynamoDB. La API key no cambia, así que los consumidores **no
necesitan hacer nada** (a diferencia de `rotate`, que sí les obliga a
actualizar su key). Acepta cualquier subconjunto de `--rate-limit`/
`--burst-limit`/`--quota-per-day` — los que omitas se quedan como estaban.
Falla si la app fue adoptada sin `UsagePlanId` conocido (no hay a qué usage
plan aplicarle el patch) o si no hay ninguna app `ACTIVE` con ese nombre.

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
```
Cuatro controles, todos atributos en DynamoDB (no en API Gateway — no requieren
redeploy ni tocan la key), aplicados por `query-lambda` en cada
`GET /documents/{id}`, `GET /documents/{id}/file`, `GET /documents?...` y
`PATCH /documents/{id}/metadata`:

| Flag | Default (app nueva) | Qué controla |
|---|---|---|
| `--access-scope own\|all` | `own` | `own`: solo documentos cuyo `tracking.appOrigin` sea la app misma. `all`: sin restricción. |
| `--allowed-doctypes A,B` | (ninguno = sin restricción) | Restringe incluso a una app `all` al consultar, descargar o editar metadata de esos tipos. No bloquea el upload. `none` en `reconfigure` quita la restricción. |
| `--allow-field-search` | `false` | Si puede usar `GET /documents?field=&value=` (el Scan-based, el más caro y abusable — ver "Cómo funciona en el backend" abajo). Sin esto, solo `docType=`/`{id}` — y `system=`/`meta.<clave>=`, que **no** están detrás de este permiso: leen una sola clave conocida del envelope en vez de recorrerlo entero, y no amplían lo que la app ya puede ver (con `AccessScope: own`, `system=` solo puede acotar más). |
| `--allow-metadata-write` | `false` | Si puede usar `PATCH /documents/{id}/metadata` para estados transitorios/marcas propios. No permite cambiar `validation.status`; sigue limitado por scope y tipos. |

**Por qué 404, no 403, en un documento fuera de alcance**: `GET /documents/{id}`
y `.../file` devuelven `404 NOT_FOUND` cuando el documento existe pero la app
no puede verlo — igual que si no existiera. No confirmar la existencia evita
que una app pueda enumerar/inferir qué IDs son válidos aunque no pueda leerlos.
`GET /documents?...` (listar/buscar) simplemente omite esos resultados de
`matches`, sin marcar error. Un `docType=` o `field=` explícitamente prohibido
sí devuelve `403 FORBIDDEN` con el motivo — ahí la app SÍ sabe qué pidió, así
que ocultarlo no suma nada.

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
(scope, field-search, metadata-write, tipos) y qué va a cambiar, con la misma confirmación
`(S/n)` que el resto de comandos. `--field-search` es un flag sin valor cuyo
sentido lo fija el comando: en `grant` siempre habilita, en `deny` siempre
deshabilita. `--metadata-write` funciona igual. `--access-scope own|all` sí toma un valor explícito en ambos
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
5. `apigateway create-usage-plan` — crea el usage plan con el
   rate/burst/quota pedidos, ya asociado al `RestApiId`+stage del paso 1-2.
6. `apigateway create-usage-plan-key` — conecta la ApiKey del paso 3 al
   usage plan del paso 5 (sin esto, la key existiría pero no tendría
   throttle/cuota aplicados — seguiría exigiendo `apiKeyRequired`, pero sin
   límites).
7. `dynamodb put-item` en `document-validation-apps[-dev]` — la única
   escritura que no es contra API Gateway: guarda `ApiKeyId → AppName` (y
   los límites/estado) para que el Upload Lambda pueda resolver el nombre
   después.

Del lado de **procesar un documento** (no del script, corre en cada request):

1. API Gateway valida `x-api-key` contra sus propios recursos ApiKey/UsagePlan
   — **nativo, antes de invocar cualquier Lambda**. Si falla, 403, cero costo.
2. Si pasa, el Upload Lambda (`upload-lambda/index.mjs`) lee
   `event.requestContext.identity.apiKeyId` (API Gateway se lo entrega
   automáticamente en el evento cuando el método exige key) y hace un
   `GetItem` **directo** (no Scan) contra `document-validation-apps[-dev]`
   por ese `ApiKeyId` — de ahí la elección de `ApiKeyId` como partition key:
   este es el único lookup que corre en el hot path de cada subida.
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
   mismo shape en ambos. Ver `documentacion/API_USO.md`.

Si algo de esa cadena falla (permiso, key no registrada, S3 metadata perdida),
el diseño es **fail-open a nivel de trazabilidad**: el documento se procesa
igual, solo con `appOrigin` ausente o `"unregistered"` — nunca se bloquea una
subida por no poder resolver quién la hizo.

**Del lado de leer o editar metadata** (`query-lambda`, cada `GET /documents*`
y `PATCH /documents/{id}/metadata`): resuelve el
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
fallo al leer AppsTable reciben `AllowMetadataWrite=false`: una degradación de
permisos nunca autoriza una mutación. En dev abierto (sin key) sí se permite el
PATCH para facilitar pruebas; en prod siempre hay key.

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
- **Regla rate-based** por IP (default 2000 req/5min, configurable con `wafRateLimit`).
- **Managed rules** de AWS: Common, KnownBadInputs, AmazonIpReputationList.
- **Usage plan**: throttle (default 20 req/s, burst 40) + cuota diaria (5000), por key.
- **AWS Shield Standard**: protección L3/L4 automática y gratis (sin config).

Tráfico bloqueado por WAF devuelve 403 **antes** de la integración → tampoco
consume Lambda.

### Configurable
En `bin/app.ts` / props del stack: `wafRateLimit`, `apiThrottle`
(`rateLimit`/`burstLimit`/`quotaPerDay`). Volumen actual es bajo (~170 pedimentos/mes),
así que los defaults son holgados.

## Notas
- Activar API keys en prod es **breaking** para consumidores actuales: deben
  empezar a mandar `x-api-key`. `scripts/enviar-documento.sh` ya soporta `API_KEY`.
- API key = identificación + rate-limit + gate sobre TLS; no es auth fuerte. Si
  luego se requiere, se puede subir a IAM/Lambda authorizer sin rehacer el resto.
- **Esto no es solo teórico desde `GET /documents/{id}/file`** (descarga del
  archivo original vía URL prefirmada de S3, ver `API_USO.md` §3): ninguna
  ruta de lectura (`GET /documents/{id}`, `GET /documents/{id}/file`,
  `GET /documents`) valida que la app dueña de la `x-api-key` sea la misma
  que subió el documento — `tracking.appOrigin` (arriba) es trazabilidad para
  auditoría posterior, nunca una ACL evaluada en el request. Con **una sola
  key compartida "por sistema"** (en vez de una key por app registrada vía
  `pnpm apps:register`), cualquier proceso que tenga esa key puede listar,
  consultar y descargar **cualquier documento de ese ambiente**, incluyendo
  archivos originales que pueden contener PII (identificaciones, RFC/CURP,
  montos, direcciones) — no solo los que ese mismo proceso subió. Si eso
  importa (varios sistemas/clientes comparten un ambiente y sus documentos
  deben estar aislados entre sí, no solo etiquetados), las opciones reales
  hoy son: (a) una key por sistema **y** un stack separado por sistema si el
  aislamiento debe ser real, no solo lógico; (b) agregar una validación de
  `appOrigin` en `query-lambda` antes de responder (no implementada — cambio
  de código); (c) un authorizer Lambda/IAM si se necesita autorización real
  por sujeto. Una URL prefirmada, además, es una credencial de acceso en sí
  misma una vez emitida — válida hasta que expira (default 15 min, tope 1h en
  `/file`) para quien sea que la tenga, sin volver a pasar por `x-api-key`.
