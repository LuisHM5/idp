# Catálogos de Tipos Documentales

Este directorio contiene archivos JSON con definiciones de tipos documentales
que se sincronizan a DynamoDB para uso en runtime (sin redeploy).

## Subir un catálogo a DynamoDB

```bash
# Subir un archivo específico
pnpm db-sync --file catalogs/curp.json

# Subir todos los tipos del config local
pnpm db-sync --all
```

Efecto inmediato — no requiere redeploy. DynamoDB siempre tiene prioridad sobre el JSON local.

## Formato del JSON

Cada archivo es un objeto donde la key es el `type_id` (MAYUSCULAS, guiones bajos):

```json
{
  "MI_TIPO": {
    "description": "Descripción del documento",
    "extraction_fields": ["CAMPO1", "CAMPO2", "CAMPO3"],
    "extraction_hints": {
      "CAMPO1": "Dónde encontrar este campo en el documento",
      "CAMPO2": "Formato esperado y ubicación visual"
    },
    "required_fields": ["CAMPO1"],
    "validation_rules": [
      {
        "field": "CAMPO1",
        "rule_type": "regex",
        "pattern": "^[A-Z]+$",
        "message": "Mensaje de error cuando falla"
      }
    ],
    "validator_module": "validators.identification"
  }
}
```

## Campos del JSON

| Campo | Tipo | Requerido | Descripción |
|-------|------|-----------|-------------|
| `description` | string | No | Descripción legible del tipo |
| `extraction_fields` | string[] | Sí | Campos que Bedrock debe extraer |
| `extraction_hints` | object | No | Pistas por campo (dónde buscar en el documento) |
| `required_fields` | string[] | Sí | Campos obligatorios (se valida presencia) |
| `validation_rules` | array | Sí | Reglas de validación determinística |
| `validator_module` | string | No | Módulo Python de validación |
| `custom_validator` | string | No | Script Node.js custom (sin .mjs) |

## Tipos de reglas

| rule_type | Descripción | Requiere `pattern` |
|-----------|-------------|-------------------|
| `regex` | Valida contra expresión regular | Sí |
| `date_expiry` | Verifica fecha no vencida | No |
| `presence` | Verifica campo no vacío | No |
| `boolean_true` | Verifica valor afirmativo (SI/TRUE/1) | No |

## Ejemplo: Crear un nuevo tipo

1. Crear archivo `catalogs/acta_nacimiento.json`:

```json
{
  "ACTA_NACIMIENTO": {
    "description": "Acta de nacimiento",
    "extraction_fields": [
      "NOMBRE_COMPLETO", "CURP", "FECHA_NACIMIENTO",
      "ENTIDAD", "MUNICIPIO", "NUMERO_ACTA"
    ],
    "extraction_hints": {
      "NOMBRE_COMPLETO": "Nombre completo del registrado",
      "CURP": "CURP de 18 caracteres, parte inferior del acta",
      "NUMERO_ACTA": "Número de acta, parte superior derecha"
    },
    "required_fields": ["NOMBRE_COMPLETO", "CURP"],
    "validation_rules": [
      {
        "field": "CURP",
        "rule_type": "regex",
        "pattern": "^[A-Z][AEIOU][A-Z]{2}\\d{6}[HM][A-Z]{5}[A-Z0-9]\\d$",
        "message": "CURP inválida"
      }
    ],
    "validator_module": "validators.identification"
  }
}
```

2. Subir a DynamoDB:

```bash
pnpm db-sync --file catalogs/acta_nacimiento.json
```

3. Listo — efecto inmediato.

## Actualizar un tipo existente

Simplemente edita el JSON y vuelve a ejecutar `pnpm db-sync --file catalogs/mi_tipo.json`.
Si el tipo ya existe en DynamoDB, se sobrescribe completamente.

## Listar tipos en DynamoDB

```bash
pnpm db-list-types
```

## Prioridad de carga

1. **DynamoDB** (máxima prioridad) — si el tipo existe aquí, se usa este
2. **JSON local** (`config/document_types.json`) — fallback si no está en DynamoDB
