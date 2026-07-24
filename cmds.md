# Comandos de catálogo (esquemas)

Solo dos, todo sale de la carpeta `catalogs/`:

```bash
pnpm sync                              # sube TODA la carpeta catalogs/ a DynamoDB (hot-reload)
pnpm sync:one -- --type PEDIMENTO_ADUANAL   # sube un solo tipo (o --file, o un nombre difuso)
pnpm sync -- --dry-run                 # valida sin escribir
```

Para hornear en la Lambda (permanente): `pnpm run build && pnpm run deploy`.
Guía para crear un tipo nuevo: `catalogs/README.md`.
