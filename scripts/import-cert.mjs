#!/usr/bin/env node
/**
 * Import (or renew) a bring-your-own TLS certificate into AWS ACM for the
 * custom API domain (idp.alsuper.com). ACM import is an API call, not a
 * CloudFormation resource, so this runs it and prints the resulting ARN.
 *
 * First import:
 *   node scripts/import-cert.mjs --cert idp.cert --key idp.key [--chain chain.pem]
 *   → prints the ARN. Paste it into .env.production as CERT_ARN, then deploy:prod.
 *
 * Renew WITHOUT downtime (re-import to the SAME ARN — no deploy, no DNS change;
 * API Gateway picks up the new cert automatically because the ARN is unchanged):
 *   node scripts/import-cert.mjs --renew --arn "$CERT_ARN" \
 *     --cert nuevo.cert --key nuevo.key [--chain chain.pem]
 *
 * Options:
 *   --cert    Path to the certificate PEM (required)
 *   --key     Path to the private key PEM (required)
 *   --chain   Path to the CA chain PEM (optional but recommended)
 *   --renew   Re-import to an existing ARN (requires --arn)
 *   --arn     Existing ACM certificate ARN to renew in place
 *   --region  AWS region (default: AWS_REGION env or us-east-1)
 *
 * The .cert/.key are secrets — keep them out of git (see .gitignore).
 */

import { existsSync } from "fs";
import { execFileSync } from "child_process";

function parseArgs(argv) {
  const o = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith("--")) {
      const k = argv[i].slice(2);
      if (k === "renew") o.renew = true;
      else if (i + 1 < argv.length && !argv[i + 1].startsWith("--")) o[k] = argv[++i];
      else o[k] = true;
    }
  }
  return o;
}

function main() {
  const a = parseArgs(process.argv.slice(2));
  const region = a.region || process.env.AWS_REGION || "us-east-1";

  if (!a.cert || !a.key) {
    console.error("Falta --cert y/o --key.\n");
    console.error("Uso: node scripts/import-cert.mjs --cert idp.cert --key idp.key [--chain chain.pem]");
    console.error("Renovar: node scripts/import-cert.mjs --renew --arn <ARN> --cert nuevo.cert --key nuevo.key");
    process.exit(2);
  }
  for (const [flag, p] of [["--cert", a.cert], ["--key", a.key], ...(a.chain ? [["--chain", a.chain]] : [])]) {
    if (!existsSync(p)) {
      console.error(`No existe el archivo de ${flag}: ${p}`);
      process.exit(2);
    }
  }
  if (a.renew && !a.arn) {
    console.error("--renew requiere --arn <ARN existente> (re-importa al mismo ARN, sin downtime).");
    process.exit(2);
  }

  const args = [
    "acm", "import-certificate",
    "--certificate", `fileb://${a.cert}`,
    "--private-key", `fileb://${a.key}`,
    "--region", region,
    "--output", "text",
    "--query", "CertificateArn",
  ];
  if (a.chain) args.push("--certificate-chain", `fileb://${a.chain}`);
  if (a.renew) args.push("--certificate-arn", a.arn);

  try {
    const arn = execFileSync("aws", args, { encoding: "utf8" }).trim();
    if (a.renew) {
      console.log(`\n✅ Cert renovado en el mismo ARN (sin downtime, sin deploy):\n   ${arn}`);
      console.log("   API Gateway tomará el cert nuevo automáticamente.\n");
    } else {
      console.log(`\n✅ Cert importado. ARN:\n   ${arn}\n`);
      console.log("   Pega esto en .env.production:");
      console.log(`   CERT_ARN=${arn}`);
      console.log("   Luego: pnpm run deploy:prod\n");
    }
  } catch (e) {
    console.error(`\n❌ Falló el import: ${e.stderr?.toString().trim() || e.message}`);
    process.exit(1);
  }
}

main();
