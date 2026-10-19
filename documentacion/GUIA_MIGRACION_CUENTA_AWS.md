# Guía simple: migrar IDP/GDI entre cuentas AWS

Esta es la secuencia operativa para mover **datos de producción y desarrollo**
desde una cuenta AWS a otra. Los tres comandos `migration:*` abren un asistente
interactivo: usa ↑/↓, Enter y Tab para completar rutas. El [plan técnico](PLAN_MIGRACION_CUENTA_AWS.md)
explica los detalles de infraestructura y las limitaciones del respaldo.

> **Estado de este procedimiento:** `migration:backup` crea un archivo local
> cifrado; `migration:restore` crea tablas DynamoDB y carga objetos en buckets
> **ya existentes**. Restaurar datos no despliega la aplicación, no cambia DNS y
> no recrea claves de API. **El modo CDK que reutiliza los datos restaurados
> aún no está implementado**: después del paso 4, no ejecutes
> `pnpm run deploy:dev` ni `pnpm run deploy:prod` hasta completar el paso 5.

## Antes de comenzar

- Trabaja desde la raíz del repositorio, con `pnpm`, `.venv`, OpenSSL y las
  dependencias del proyecto instaladas. Si falta algo, sigue la
  [instalación del README](../README.md#instalación-y-primer-despliegue).
- Ten un perfil AWS para el **origen** y otro para el **destino**, con permisos
  suficientes en `us-east-1`. El asistente muestra el ID real de cada cuenta.
  En el caso actual, `alsuper` corresponde al origen `886031931562`; el perfil
  destino debe mostrar **otro** ID. Un ID de 12 dígitos no es un perfil.
- Reserva espacio local para el archivo cifrado y para descomprimirlo durante
  la restauración. El asistente propone `/tmp` para archivos temporales; allí
  habrá datos sin cifrar mientras trabaja. Puedes elegir un volumen cifrado.
- No ejecutes el respaldo final mientras la aplicación siga escribiendo en
  DynamoDB o S3: `Scan` consistente no es una instantánea de toda la tabla.

## 1. Inventariar la cuenta origen

```bash
pnpm migration:plan
```

Selecciona `alsuper` (o el perfil origen correcto), `us-east-1` y `both`.
Escribe la confirmación que muestra el asistente.

**Éxito:** aparece el ID de la cuenta origen y la lista de tablas DynamoDB y
buckets documentales de prod y dev. Si el ID no coincide con el esperado,
detente antes de crear el respaldo.

## 2. Preparar la cuenta destino

En la consola AWS de la **cuenta nueva**, en la misma región del respaldo:

1. Crea **dos buckets documentales** con nombres nuevos, uno para `prod` y otro
   para `dev`. Deben estar vacíos y sin notificaciones S3. Activa bloqueo de
   acceso público y el cifrado que corresponda. Anota ambos nombres.
2. Prepara una clave KMS **de la cuenta destino** para las tablas productivas
   cifradas con KMS. Anota su ARN y verifica que el perfil destino pueda usarla.
3. Deja sin crear las tablas DynamoDB que se van a importar. Si desplegaste el
   CDK y esas tablas ya existen, la restauración se detendrá en la revisión
   previa. Reconcílialas con el [plan técnico](PLAN_MIGRACION_CUENTA_AWS.md#7-fase-c-restauración-en-la-cuenta-independiente)
   antes de continuar.

**Éxito:** hay dos buckets destino vacíos, sin notificaciones; las tablas
destino todavía no existen y conoces el perfil de la cuenta nueva y, si
aplica, el ARN KMS de producción.

## 3. Detener escrituras y crear el respaldo final

Detén temporalmente cargas, modificaciones y registro de referencias en la
cuenta origen. Espera a que terminen los documentos en proceso. Entonces:

```bash
pnpm migration:backup
```

Selecciona origen, región y `both`. Crea una contraseña nueva y guárdala en un
gestor de contraseñas. Enter acepta el archivo cifrado sugerido en tu carpeta
personal y `/tmp` como carpeta temporal. La **segunda copia es opcional** en el
asistente; si la haces para proteger el corte final, guárdala en **otro disco
o cuenta**. `~/Downloads/output` y un archivo en `~` pueden estar en el mismo
sistema de archivos: no son copias independientes ante una falla del disco.

**Éxito:** el asistente muestra `Respaldo cifrado: ... SHA256=...` y los conteos
de tablas y objetos. Conserva la ruta exacta, el SHA-256 y la contraseña.
El archivo puede tener otro nombre o extensión: la copia sigue siendo el mismo
respaldo cifrado.

> Si ya tienes `/home/luis-pc/aws-migration-backup-20261019-211442.tar.gz.enc`,
> úsalo para la importación definitiva **solo si** se creó con las escrituras
> detenidas. Si había actividad, crea primero otro respaldo final. El asistente
> no reemplaza tablas u objetos que ya importaste en destino.

## 4. Importar los datos en la cuenta nueva

Con los buckets destino preparados, ejecuta el asistente con la ruta de tu
archivo cifrado. Para el respaldo actual:

```bash
pnpm migration:restore --archive /home/luis-pc/aws-migration-backup-20261019-211442.tar.gz.enc
```

Responde la contraseña original, elige el **perfil destino** y comprueba el ID
de cuenta que muestra STS. Escribe los nombres de los buckets vacíos de `prod`
y `dev`, y el ARN de la clave KMS nueva si lo solicita. **No hace falta crear
un JSON de buckets.** El asistente verifica el contenido del archivo, las
tablas y los buckets antes de pedir `RESTAURAR <ID de la cuenta destino>`.

**Éxito:** aparecen `Checksum verificado`, una línea `Restaurada: ...` por
tabla, `Restaurado S3: ...` por bucket y al final `Datos cargados. Falta
adoptar tablas en IaC y validar la aplicación.` Si falla después de empezar a
escribir, conserva los recursos creados para inspección: el asistente no los
borra ni reanuda sobre tablas existentes.

## 5. Conectar la infraestructura y validar

**Aquí hay una tarea de código pendiente.** El CDK actual crea sus propias
tablas y su propio bucket. Si se corre `pnpm run deploy:prod` tal cual después
de restaurar, las tablas con nombre fijo pueden chocar y el stack puede crear
un bucket o una tabla de seguimiento vacíos. La aplicación podría quedar
conectada a esos recursos en lugar de los restaurados. `deploy:dev` presenta
el mismo problema.

Antes del deploy, adaptar el CDK para **referenciar los nombres físicos
restaurados** de todas las tablas y buckets de cada ambiente, con permisos
IAM/KMS de la nueva cuenta; como alternativa, importarlos en un data stack y
referenciarlos desde la aplicación. Este modo de adopción todavía no existe en
el repositorio. Configurar `.env.dev` y `.env.production` para el destino; el
certificado ACM de la cuenta origen no sirve en la nueva. El
[plan técnico, fase C](PLAN_MIGRACION_CUENTA_AWS.md#7-fase-c-restauración-en-la-cuenta-independiente)
enumera los recursos y comprobaciones concretas.

**Solo cuando esa adaptación esté implementada y verificada**, con un perfil
`destino` que realmente apunta a la cuenta nueva, revisar y desplegar en este
orden:

```bash
AWS_PROFILE=destino pnpm run diff:dev
AWS_PROFILE=destino pnpm run diff:prod
AWS_PROFILE=destino pnpm run deploy:dev
AWS_PROFILE=destino pnpm run deploy:prod
```

Sustituye `destino` por el nombre real de tu perfil. Antes de cada despliegue,
verifica el ID de cuenta y que el `diff` no cree otro bucket/tabla documental
ni reemplace datos. Después recrea las API keys y usage plans, secretos,
certificados/DNS y suscripciones que el respaldo no contiene. Si usas S3
Vectors, reconstruye el índice desde las referencias restauradas.

**Éxito de infraestructura:** ambos stacks llegan a `CREATE_COMPLETE` o
`UPDATE_COMPLETE`; las Lambdas apuntan a los **mismos** buckets y tablas que
restauraste, y solo `source/` y `raw/` invocan procesamiento. El endpoint nuevo
permanece sin tráfico de producción hasta terminar la validación.

Comprueba en **ambos** ambientes:

| Acción | Señal de éxito |
|---|---|
| Consultar un documento histórico | Devuelve su resultado y permite descargar el original. |
| Subir un documento pequeño y consultar su resultado | Termina el procesamiento sin error y aparece el resultado. |
| Usar una API key de prueba | Autentica y conserva los permisos esperados. |
| Consultar referencias, catálogos y costos históricos | Los registros restaurados están presentes. |
| Revisar DynamoDB y S3 | Conteos, claves e inventario coinciden con el manifiesto del respaldo. |

**Éxito:** los datos históricos y un documento nuevo funcionan en destino,
sin escrituras accidentales hacia la cuenta origen. `migration:restore` verifica
conteos cargados y tamaños S3, pero esta validación funcional sigue siendo
necesaria.

## 6. Cambiar tráfico y cerrar la migración

Solo después de la validación, cambia el dominio o los consumidores al endpoint
de la cuenta nueva. Mantén la cuenta origen disponible y sin escrituras como
respaldo para revertir el cambio si aparece un fallo. Guarda una **segunda
copia independiente** del archivo cifrado antes de retirar la cuenta origen.

**Éxito:** las peticiones nuevas llegan al destino, los documentos históricos
siguen accesibles y existe una ruta de reversión probada. El traspaso de datos
no completa por sí solo la atribución de costos/CUR 2.0; esa fase está descrita
en el [plan técnico](PLAN_MIGRACION_CUENTA_AWS.md#9-atribución-de-costos-sin-depender-de-tags).
