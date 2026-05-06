# Custom Validators (Node.js)

Este directorio contiene scripts de validación personalizados en Node.js
que se ejecutan como paso adicional después de las validaciones estándar.

## ¿Cuándo usar custom validators?

Cuando necesitas lógica de validación que va más allá de regex/fechas/presencia:
- Consultar APIs externas (ej: validar RFC contra SAT)
- Lógica de negocio compleja (ej: cruce entre documentos)
- Cálculos numéricos (ej: verificar dígito verificador)
- Integraciones con sistemas internos

## Cómo agregar un custom validator

1. Crea un archivo `.mjs` en este directorio (ej: `validate_rfc_sat.mjs`)
2. Exporta una función `validate(entityMap, context)` que retorne un array de checks
3. Referencia el script en el JSON config: `"custom_validator": "validate_rfc_sat"`

## Interfaz del script

```javascript
// validate_rfc_sat.mjs
export async function validate(entityMap, context) {
  // entityMap: { "RFC": "XAXX010101000", "CURP": "...", ... }
  // context: { documentId, classification, typeConfig }
  
  const checks = [];
  
  // Tu lógica aquí...
  checks.push({
    field: "RFC",
    rule: "custom:sat_lookup",
    valid: true,
    message: "RFC encontrado en registros del SAT"
  });
  
  return checks;
}
```

## Retorno esperado

Array de objetos con la estructura:
```json
[
  {
    "field": "NOMBRE_CAMPO",
    "rule": "custom:nombre_regla",
    "valid": true | false,
    "message": "Descripción del resultado"
  }
]
```

## Ejemplo: Validar dígito verificador de CURP

```javascript
// validate_curp_checkdigit.mjs
export async function validate(entityMap, context) {
  const curp = entityMap["CURP"];
  if (!curp || curp.length !== 18) return [];
  
  const checkDigit = calculateCurpCheckDigit(curp.substring(0, 17));
  const valid = checkDigit === parseInt(curp[17]);
  
  return [{
    field: "CURP",
    rule: "custom:checkdigit",
    valid,
    message: valid 
      ? "Dígito verificador de CURP correcto" 
      : "Dígito verificador de CURP incorrecto"
  }];
}
```

## Variables de entorno disponibles

- `CATALOG_TABLE`: Nombre de la tabla DynamoDB del catálogo
- `DOCUMENT_BUCKET`: Bucket S3 donde se almacenan documentos
- Cualquier variable configurada en el CDK stack
