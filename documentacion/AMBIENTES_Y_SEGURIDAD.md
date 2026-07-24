# Ambientes, dominio propio y seguridad

Dos ambientes, un solo código. `prod` va endurecido (dominio propio + WAF +
API keys); `dev` queda abierto en el URL random de Amazon para probar fácil.

| | dev | prod |
|---|---|---|
| Stack | `DocumentValidationStack-Dev` | `DocumentValidationStack` |
| URL | `execute-api` random de Amazon | `https://idp.alsuper.com` (o random si no configuras dominio) |
| Auth | ninguna (abierto) | **API key** (`x-api-key`) en todos los endpoints |
| Rate limit / anti-DDoS | no | **WAF** (rate-based + managed rules) + usage plan |
| TLS | sí (execute-api) | sí (cert propio, TLS 1.2+) |

## Desplegar

```bash
pnpm run deploy         # = DEV por default (seguro, nunca toca prod)
pnpm run deploy:dev     # DocumentValidationStack-Dev (abierto, DNS random)
pnpm run deploy:prod    # DocumentValidationStack (dominio + WAF + API keys) — explícito
pnpm run diff:dev / diff:prod
```

La config por ambiente vive en `.env.dev` y `.env.production` (ambos gitignored;
plantilla en `.env.example`). El ambiente se elige con `-c env=dev|prod` (lo
hacen los scripts).

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

## Seguridad (prod)

### API keys — protege y evita consumo por 401/403/404
Todos los endpoints exigen `x-api-key`. Un request sin key válida recibe **403
nativo de API Gateway sin invocar la Lambda** → **no hay consumo de backend** por
requests no autorizados o rutas inexistentes (404). Solo requests válidos (200) o
errores reales del backend consumen Lambda/Bedrock.

Obtener el valor de la key (el deploy imprime su **id** en `ApiKeyId`):
```bash
aws apigateway get-api-key --api-key <ApiKeyId> --include-value \
  --region us-east-1 --query value --output text
```
Usarla:
```bash
curl -H "x-api-key: <VALOR>" https://idp.alsuper.com/documents/...
# o con el script:
API_KEY=<VALOR> pnpm enviar archivo.pdf PEDIMENTO_ADUANAL
```
Puedes crear **una key por sistema consumidor** (Promocontrol, CEDIS/ERP, Portal…)
para métricas y límites por módulo — se agregan en el stack (`api.addApiKey` +
`usagePlan.addApiKey`).

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
