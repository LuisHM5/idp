import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";

import {
  discoverCatalogTypes,
  isCatalogPathActive,
  selectCatalogFile,
  staleEnabledTypeIds,
} from "../../../scripts/catalog-files.mjs";

function withCatalogTree(run) {
  const root = mkdtempSync(join(tmpdir(), "catalog-files-"));
  try {
    run(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

function writeType(root, relativePath, typeId) {
  const path = join(root, relativePath);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify({ [typeId]: { fields: {} } }));
  return path;
}

test("catalog discovery is recursive, deterministic, and excludes every save subtree", () => {
  withCatalogTree((root) => {
    writeType(root, "custom/erp/ZETA.json", "ZETA");
    writeType(root, "base/identidad/ALFA.json", "ALFA");
    writeType(root, "save/OCULTO.json", "OCULTO");
    writeType(root, "base/save/TAMBIEN_OCULTO.json", "TAMBIEN_OCULTO");
    writeFileSync(join(root, "_shared.json"), JSON.stringify({ _field_sets: {} }));

    assert.deepEqual(
      discoverCatalogTypes(root).map(({ typeId }) => typeId),
      ["ALFA", "ZETA"]
    );
  });
});

test("a catalog key must match its file name", () => {
  withCatalogTree((root) => {
    writeType(root, "base/identidad/ARCHIVO.json", "OTRO_TIPO");
    assert.throws(() => discoverCatalogTypes(root), /debe coincidir con el archivo/);
  });
});

test("catalog files must contain exactly one object entry", () => {
  withCatalogTree((root) => {
    mkdirSync(join(root, "base"), { recursive: true });
    writeFileSync(join(root, "base/DOS.json"), JSON.stringify({ DOS: {}, OTRO: {} }));
    assert.throws(() => discoverCatalogTypes(root), /exactamente un TYPE_ID/);
  });
  withCatalogTree((root) => {
    mkdirSync(join(root, "base"), { recursive: true });
    writeFileSync(join(root, "base/MALO.json"), JSON.stringify({ MALO: [] }));
    assert.throws(() => discoverCatalogTypes(root), /debe contener un objeto/);
  });
});

test("duplicate TYPE_IDs fail instead of silently overriding one another", () => {
  withCatalogTree((root) => {
    writeType(root, "base/identidad/TIPO.json", "TIPO");
    writeType(root, "custom/erp/TIPO.json", "TIPO");
    assert.throws(() => discoverCatalogTypes(root), /TYPE_ID duplicado 'TIPO'/);
  });
});

test("saved files are inactive even when selected by an exact path", () => {
  withCatalogTree((root) => {
    const active = writeType(root, "base/identidad/ACTIVO.json", "ACTIVO");
    const saved = writeType(root, "save/GUARDADO.json", "GUARDADO");
    assert.equal(isCatalogPathActive(active, root), true);
    assert.equal(isCatalogPathActive(saved, root), false);
    assert.equal(isCatalogPathActive(join(root, "_shared.json"), root), false);
    assert.equal(isCatalogPathActive(join(root, "..", "fuera.json"), root), false);
  });
});

test("stale-row detection ignores disabled and internal DynamoDB entries", () => {
  assert.deepEqual(
    staleEnabledTypeIds(["ACTIVO"], [
      { type_id: { S: "ACTIVO" }, enabled: { BOOL: true } },
      { type_id: { S: "OBSOLETO" }, enabled: { BOOL: true } },
      { type_id: { S: "YA_DESHABILITADO" }, enabled: { BOOL: false } },
      { type_id: { S: "_interno" }, enabled: { BOOL: true } },
    ]),
    ["OBSOLETO"]
  );
});

test("the repository ships 20 active catalogs with explicit maturity", () => {
  const types = discoverCatalogTypes(resolve("catalogs"));
  assert.equal(types.length, 20);
  assert.ok(types.every(({ entry }) => ["stable", "beta"].includes(entry.catalog_status)));

  const beta = new Map(
    types
      .filter(({ entry }) => entry.catalog_status === "beta")
      .map(({ typeId, entry }) => [typeId, entry])
  );
  assert.deepEqual([...beta.keys()].sort(), [
    "ACTA_NACIMIENTO",
    "CEDULA_PROFESIONAL",
    "CFDI_GASTO_TERCERO_XML",
    "CFDI_PAGO_DERECHOS_SANITARIOS_XML",
    "COMPROBANTE_DOMICILIO",
    "COMPROBANTE_ESTUDIOS",
    "CONSTANCIA_NSS",
    "CURP",
    "FACTURA_LOGISTICA_XML",
    "IDENTIFICACION",
    "PASAPORTE",
  ]);
  assert.equal(beta.get("COMPROBANTE_DOMICILIO").segmentation_candidate, false);
  assert.equal(beta.get("CEDULA_PROFESIONAL").segmentation_candidate, false);
  assert.equal(beta.get("COMPROBANTE_ESTUDIOS").segmentation_candidate, false);
  assert.equal(beta.get("IDENTIFICACION").segmentation_candidate, false);
  for (const typeId of ["CURP", "ACTA_NACIMIENTO", "CONSTANCIA_NSS", "PASAPORTE"]) {
    assert.notEqual(beta.get(typeId).segmentation_candidate, false);
  }
});

test("INE keeps only the essential printed identity and verification fields", () => {
  const types = discoverCatalogTypes(resolve("catalogs"));
  const ine = types.find(({ typeId }) => typeId === "INE")?.entry;
  assert.ok(ine);
  assert.deepEqual(Object.keys(ine.fields), [
    "APELLIDO_PATERNO",
    "APELLIDO_MATERNO",
    "NOMBRES",
    "CURP",
    "CLAVE_ELECTOR",
    "FECHA_NACIMIENTO",
    "VIGENCIA",
    "CIC",
    "IDENTIFICADOR_CIUDADANO",
  ]);
  assert.ok(Object.keys(ine.fields).every((field) => !field.startsWith("ANIO_")));
  assert.equal(ine.qr_recovery, undefined);
});

test("generic identification reports the decision without claiming authenticity", () => {
  const types = discoverCatalogTypes(resolve("catalogs"));
  const generic = types.find(({ typeId }) => typeId === "IDENTIFICACION")?.entry;
  assert.ok(generic);
  assert.equal(generic.segmentation_candidate, false);
  assert.equal(generic.fields.ES_DOCUMENTO_IDENTIDAD.type, "boolean");
  assert.equal(generic.fields.ES_DOCUMENTO_IDENTIDAD.required, true);
  assert.equal(generic.fields.ES_DOCUMENTO_IDENTIDAD.validation[0].rule_type, "boolean_true");
  assert.match(generic.description, /No certifica autenticidad/);
});

test("sync:one selectors resolve nested catalogs by exact or fuzzy TYPE_ID", () => {
  const catalogsDir = resolve("catalogs");
  const types = discoverCatalogTypes(catalogsDir);
  assert.match(
    selectCatalogFile({ opts: { type: "PEDIMENTO_ERP" }, positional: [] }, types, catalogsDir),
    /catalogs[/\\]custom[/\\]erp[/\\]PEDIMENTO_ERP\.json$/
  );
  assert.match(
    selectCatalogFile({ opts: {}, positional: ["constancia_nss"] }, types, catalogsDir),
    /catalogs[/\\]base[/\\]recursos_humanos[/\\]CONSTANCIA_NSS\.json$/
  );
  assert.match(
    selectCatalogFile(
      { opts: { file: "catalogs/base/identidad/CURP.json" }, positional: [] },
      types,
      catalogsDir
    ),
    /catalogs[/\\]base[/\\]identidad[/\\]CURP\.json$/
  );
  assert.throws(
    () => selectCatalogFile({ opts: {}, positional: ["pedimento"] }, types, catalogsDir),
    /es ambiguo/
  );
  assert.throws(
    () => selectCatalogFile({ opts: { type: "NO_EXISTE" }, positional: [] }, types, catalogsDir),
    /tipo no encontrado/
  );
  assert.throws(
    () => selectCatalogFile({ opts: {}, positional: [] }, types, catalogsDir),
    /indica un tipo/
  );
  assert.throws(
    () => selectCatalogFile({ opts: { name: "nada_que_exista" }, positional: [] }, types, catalogsDir),
    /ningún tipo coincide/
  );
  assert.throws(
    () => selectCatalogFile({ opts: { file: "catalogs/save/CURP.json" }, positional: [] }, types, catalogsDir),
    /archivo inactivo/
  );
  assert.throws(
    () => selectCatalogFile({ opts: { file: "catalogs/no-existe.json" }, positional: [] }, types, catalogsDir),
    /archivo no encontrado/
  );
});
