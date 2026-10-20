# Guía simple: migrar IDP/GDI entre cuentas AWS

Esta es la secuencia operativa para mover **infraestructura y datos de producción
y desarrollo** desde una cuenta AWS a otra. Los comandos `migration:*` abren un asistente
interactivo: usa ↑/↓, Enter y Tab para completar rutas. El [plan técnico](PLAN_MIGRACION_CUENTA_AWS.md)
explica los detalles de infraestructura y las limitaciones del respaldo.

> **Orden obligatorio:** primero despliega CDK en la cuenta nueva con el ingreso
> S3 pausado; después carga el respaldo en las tablas y buckets de esos stacks;
> finalmente activa las notificaciones. Desplegar sin la pausa antes de copiar
> `source/` o restaurar en tablas ajenas al stack puede reprocesar documentos o
> dejar a la aplicación leyendo tablas vacías.

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

## 2. Preparar y desplegar la infraestructura vacía en destino

Configura un perfil AWS con nombre para la **cuenta nueva**. Revisa
`.env.dev` y `.env.production`: deben contener los modelos y ajustes de la
cuenta nueva; `CERT_ARN` nunca debe ser el certificado de la cuenta origen.
Deja `DOMAIN_NAME`/`CERT_ARN` sin configurar hasta tener un certificado ACM
válido en destino. Habilita el acceso a los modelos Bedrock requeridos en
`us-east-1`. La cuenta nueva debe estar lista para CDK bootstrap. No crees
tablas ni buckets documentales a mano.

```bash
pnpm migration:deploy
```

Elige el perfil destino, región `us-east-1`, ambiente `both` y fase **Inicial:
crear infraestructura sin notificaciones S3**. El asistente comprueba y muestra
el ID real de la cuenta; escribe `DESPLEGAR <ID> PAUSADO` solo si es la cuenta
nueva. Este comando ejecuta `deploy:dev` y `deploy:prod` con ese perfil y crea
los buckets, tablas, KMS, API y Lambdas, pero todavía no conecta `source/` ni
`raw/` al procesamiento.

**Éxito:** ambos stacks están en `CREATE_COMPLETE`/`UPDATE_COMPLETE`; el
asistente termina con `Despliegue terminado`; los buckets documentales y las
tablas de ambos ambientes están vacíos y sin notificaciones S3. No compartas
todavía el endpoint nuevo con consumidores.

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

Con los stacks destino desplegados en fase pausada, ejecuta el asistente con la ruta de tu
archivo cifrado. Para el respaldo actual:

```bash
pnpm migration:restore --archive /home/luis-pc/aws-migration-backup-20261019-211442.tar.gz.enc
```

Responde la contraseña original, elige el **perfil destino** y comprueba el ID
de cuenta que muestra STS. Elige **Stacks CDK ya desplegados, con ingreso S3
pausado**. El asistente usa los IDs lógicos guardados en el respaldo para
encontrar automáticamente las tablas y buckets físicos de los nuevos stacks.
Verifica que estén vacíos, que las claves e índices de las tablas coincidan y
que los buckets no tengan notificaciones. Muestra el mapa origen → destino y
pide `RESTAURAR <ID de la cuenta destino>` antes de escribir. La CMK productiva
la creó el despliegue CDK inicial; no necesitas proporcionar su ARN aquí.

**Éxito:** aparecen `Checksum verificado`, una línea `Restaurada: ...` por
tabla, `Restaurado S3: ...` por bucket y al final `Datos cargados en los stacks
CDK`. Si falla después de empezar a escribir, conserva los recursos creados
para inspección: el asistente no borra ni reanuda sobre tablas no vacías.

## 5. Activar el procesamiento y validar

```bash
pnpm migration:deploy
```

Elige el **mismo perfil destino**, ambiente `both` y fase **Activar: conectar
notificaciones S3 tras restaurar**. Confirma el ID de cuenta y escribe
`DESPLEGAR <ID> ACTIVO`. El asistente vuelve a ejecutar los despliegues CDK,
ahora con las notificaciones `source/` y `raw/`. Los objetos históricos ya
están cargados, por lo que no se vuelven a procesar por la carga inicial.

Después recrea las API keys y usage plans, secretos, certificados/DNS y
suscripciones que el respaldo no contiene. Los `ApiKeyId` de la cuenta origen
no funcionan en la nueva: reconcilia `AppsTable` con las claves nuevas. Si
usas S3 Vectors, reconstruye el índice desde las referencias restauradas.

**Éxito de infraestructura:** ambos stacks llegan a `CREATE_COMPLETE` o
`UPDATE_COMPLETE`; las Lambdas siguen apuntando a los **mismos** buckets y
tablas donde restauraste los datos, y solo `source/` y `raw/` invocan
procesamiento. El endpoint nuevo permanece sin tráfico de producción hasta
terminar la validación.

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
