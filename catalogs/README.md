# `catalogs/` — Base única de esquemas

Esta carpeta es la **única fuente de verdad** de los tipos de documento. Un
archivo JSON por tipo, más un archivo compartido. No hay otro lugar donde se
definan tipos.

```
catalogs/
├── _shared.json      → globals: _field_sets, cross_document_validation_rules, _global_output_fields
├── _TEMPLATE.json    → plantilla para crear un tipo nuevo (se ignora en build/sync)
├── PEDIMENTO_ADUANAL.json
├── INE.json
└── <TYPE_ID>.json    → un tipo por archivo, shape { "<TYPE_ID>": { ...entry } }
```

- El **nombre del archivo** es el `TYPE_ID` (UPPER_SNAKE_CASE): `PEDIMENTO_ADUANAL.json`.
- Los archivos que empiezan con `_` son especiales (no son tipos).

## Los 2 únicos comandos

```bash
pnpm sync                              # sube TODA la carpeta catalogs/ a DynamoDB (hot-reload)
pnpm sync:one -- PEDIMENTO              # sube UN tipo (por nombre difuso)
pnpm sync:one -- --type INE             # por TYPE_ID exacto
pnpm sync:one -- --file catalogs/X.json # por archivo
```

Opciones: `--dry-run` (no escribe), `--table`, `--region`.

`sync`/`sync:one` escriben a **DynamoDB** → efecto **inmediato, sin redeploy**.
Para hornear los cambios en la Lambda (línea base permanente) usa el build normal:

```bash
pnpm run build && pnpm run deploy
```

`pnpm run build` combina automáticamente `catalogs/*.json` en el catálogo
empaquetado (`config/catalogs.json`). DynamoDB (hot-reload) siempre gana sobre
lo empaquetado en runtime.

## Crear un tipo nuevo (3 pasos)

1. **Copia la plantilla** a un archivo con el nombre del tipo:
   ```bash
   cp catalogs/_TEMPLATE.json catalogs/ACTA_NACIMIENTO.json
   ```
2. **Edita** el archivo: cambia `NUEVO_TIPO` por `ACTA_NACIMIENTO`, ajusta
   `description` (va al prompt de clasificación), `extraction_fields`,
   `extraction_hints`, `required_fields`, `validation_rules`. Borra la clave
   `__NOTAS__`.
3. **Aplícalo**:
   ```bash
   pnpm sync:one -- --type ACTA_NACIMIENTO     # inmediato (DynamoDB), o
   pnpm run build && pnpm run deploy           # permanente (empaquetado)
   ```

### Campos del entry

| Campo | Obligatorio | Qué es |
|---|---|---|
| `description` | sí | Qué es el documento; se usa para clasificar (sé discriminante). |
| `extraction_fields` | sí | Lista de campos a extraer (UPPER_SNAKE_CASE). |
| `extraction_hints` | no | `campo → pista` en español de dónde vive el dato. |
| `required_fields` | no | Campos que deben existir para aprobar. |
| `validation_rules` | no | Reglas: `regex`, `date_expiry`, `presence`, `custom_formula`, `sum_array`, ... |
| `validator_module` | no | Módulo Python de validación (`validators.identification`, etc.). |
| `custom_validator` | no | Script Node en `custom_validators/` para lógica avanzada. |

### Solo para tipos aduanales multi-documento

| Campo | Qué hace |
|---|---|
| `"pipeline": "expediente"` | Enruta al pipeline de expediente (segmentación + reglas cruzadas). Sin él, el tipo usa el flujo estándar de un solo documento. |
| `"field_sets": ["COMMON_CFDI_FIELDS"]` | Reutiliza listas de campos definidas en `_shared.json`. |
| `repeatable_fields`, `structured_fields`, `form_mapping`, `schema_version`, `business_destination`, `min_confidence` | Config avanzada — ver `PEDIMENTO_ADUANAL.json` como ejemplo. |

Las reglas **cross-documento** (mismo pedimento, RFC importador, etc.) viven en
`_shared.json` bajo `cross_document_validation_rules`.
