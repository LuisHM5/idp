# API IDP — Ejemplo completo en C#

Ejemplo para integrar una aplicación .NET con IDP usando
`PEDIMENTO_ERP`. El flujo registra el documento con metadata, sube el PDF
directamente a S3, espera el resultado, modifica un estado externo, busca el
documento y descarga el archivo original.

Requiere .NET 6 o posterior. Usa el `HttpClient` estándar y
`System.Text.Json`: no necesita el SDK de AWS ni paquetes NuGet adicionales.
El código sirve igual desde una consola, un Worker Service, ASP.NET Core o
cualquier otra aplicación .NET.

## 1. Preparación

Configura la URL del ambiente y la API key asignada a tu aplicación:

~~~bash
export IDP_API_URL="https://idp.alsuper.com"
export IDP_API_KEY="<API_KEY_DE_LA_APP>"
~~~

La app debe tener permiso de escritura de metadata para ejecutar el PATCH:

~~~bash
pnpm apps:grant -- --name ERP --env prod --metadata-write
~~~

Crea el proyecto:

~~~bash
dotnet new console -n IdpEjemplo
cd IdpEjemplo
~~~

Reemplaza el contenido de `Program.cs` por el siguiente programa.

## 2. Programa completo

~~~csharp
using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;

var api = RequiredEnvironment("IDP_API_URL").TrimEnd('/');
var apiKey = RequiredEnvironment("IDP_API_KEY");
var filePath = args.Length > 0 ? args[0] : "pedimento.pdf";

if (!File.Exists(filePath))
{
    throw new FileNotFoundException(
        "Indica el PDF como primer argumento.", filePath);
}

// Este cliente se usa únicamente contra el API IDP.
using var apiClient = new HttpClient();
apiClient.DefaultRequestHeaders.Add("x-api-key", apiKey);

// Cliente separado para S3: nunca lleva x-api-key ni cabeceras x-amz-*.
using var s3Client = new HttpClient();

// -------------------------------------------------------------------------
// 1. Registrar el documento. Este es el body HTTP completo del POST.
// -------------------------------------------------------------------------
var registerBody = new
{
    fileName = Path.GetFileName(filePath),
    documentType = "PEDIMENTO_ERP",
    metadata = new
    {
        lote = "A123",
        folio_erp = "4711",
        estado_externo = "RECIBIDO",
        requiere_atencion = false
    }
};

using var registerResponse = await apiClient.PostAsJsonAsync(
    $"{api}/documents",
    registerBody);
await EnsureSuccess(registerResponse);

var registration = await ReadJson(registerResponse);
var documentId = registration.GetProperty("documentId").GetString()
    ?? throw new InvalidOperationException("La respuesta no contiene documentId.");
var uploadUrl = registration.GetProperty("uploadUrl").GetString()
    ?? throw new InvalidOperationException("La respuesta no contiene uploadUrl.");

Console.WriteLine($"Documento registrado: {documentId}");

// -------------------------------------------------------------------------
// 2. Subir los bytes directamente a S3.
// No agregues Content-Type, x-api-key ni cabeceras x-amz-*.
// -------------------------------------------------------------------------
await using (var input = File.OpenRead(filePath))
using (var uploadContent = new StreamContent(input))
using (var uploadResponse = await s3Client.PutAsync(uploadUrl, uploadContent))
{
    await EnsureSuccess(uploadResponse);
}

Console.WriteLine("Archivo subido; el pipeline inició automáticamente.");

// -------------------------------------------------------------------------
// 3. Consultar hasta un estado terminal.
// 202 significa que sigue procesándose; 404 sí significa que no existe/no se ve.
// El mismo GET espera hasta 20 s dentro del servidor; no existe una ruta fast.
// -------------------------------------------------------------------------
var result = await WaitForResult(
    apiClient,
    api,
    documentId,
    attempts: 90,
    delay: TimeSpan.FromSeconds(1));

var validationStatus = result
    .GetProperty("result")
    .GetProperty("validation")
    .GetProperty("status")
    .GetString();

Console.WriteLine($"Validación IDP: {validationStatus}");

// -------------------------------------------------------------------------
// 4. Actualizar metadata externa.
// Este es el body HTTP completo del PATCH. Las claves no enviadas se
// conservan; null o string vacío eliminan una clave.
// -------------------------------------------------------------------------
var patchBody = new Dictionary<string, object?>
{
    ["estado_externo"] = "ENVIADO_A_ERP",
    ["folio_erp"] = "4711",
    ["requiere_atencion"] = null
};

using var patchRequest = new HttpRequestMessage(
    HttpMethod.Patch,
    $"{api}/documents/{Uri.EscapeDataString(documentId)}/metadata")
{
    Content = new StringContent(
        JsonSerializer.Serialize(patchBody),
        Encoding.UTF8,
        "application/merge-patch+json")
};

using var patchResponse = await apiClient.SendAsync(patchRequest);
await EnsureSuccess(patchResponse);

var patched = await ReadJson(patchResponse);
Console.WriteLine(
    $"Metadata versión {patched.GetProperty("metadataVersion").GetInt32()}:");
Console.WriteLine(Pretty(patched.GetProperty("metadata")));

// -------------------------------------------------------------------------
// 5. Buscar usando todos los grupos principales de parámetros.
// Los parámetros repetidos se conservan en el orden indicado.
// -------------------------------------------------------------------------
var searchUrl = BuildUrl(
    $"{api}/documents",
    ("docType", "PEDIMENTO_ERP"),
    ("system", "ERP"),
    ("meta.lote", "A123"),
    ("meta.estado_externo", "ENVIADO_A_ERP"),
    ("metaOp", "eq"),
    ("field", "PROVEEDOR_ID_FISCAL"),
    ("value", "OFU910626UQ0"),
    ("op", "eq"),
    ("status", "APROBADO"),
    ("since", "2026-09-01T00:00:00Z"),
    ("until", "2026-09-30T23:59:59Z"),
    ("order", "desc"),
    ("expand", "true"),
    ("limit", "25"),
    ("summary", "false"),
    ("full", "false"));

using var searchResponse = await apiClient.GetAsync(searchUrl);
await EnsureSuccess(searchResponse);
var searchResult = await ReadJson(searchResponse);

Console.WriteLine(
    $"Coincidencias: {searchResult.GetProperty("count").GetInt32()}");

// Búsqueda rápida por la unique key de PEDIMENTO_ERP:
var byNumberUrl = BuildUrl(
    $"{api}/documents",
    ("docType", "PEDIMENTO_ERP"),
    ("field", "No_Pedimento"),
    ("value", "26 07 0823 6001630"),
    ("op", "eq"),
    ("limit", "25"),
    ("summary", "true"),
    ("full", "false"));

using var byNumberResponse = await apiClient.GetAsync(byNumberUrl);
await EnsureSuccess(byNumberResponse);
Console.WriteLine(Pretty(await ReadJson(byNumberResponse)));

// -------------------------------------------------------------------------
// 6. Obtener una URL temporal y descargar el archivo original.
// -------------------------------------------------------------------------
var fileInfoUrl = BuildUrl(
    $"{api}/documents/{Uri.EscapeDataString(documentId)}/file",
    ("expiresIn", "900"));

using var fileInfoResponse = await apiClient.GetAsync(fileInfoUrl);
await EnsureSuccess(fileInfoResponse);
var fileInfo = await ReadJson(fileInfoResponse);
var downloadUrl = fileInfo.GetProperty("downloadUrl").GetString()
    ?? throw new InvalidOperationException("La respuesta no contiene downloadUrl.");

using var downloadResponse = await s3Client.GetAsync(
    downloadUrl,
    HttpCompletionOption.ResponseHeadersRead);
await EnsureSuccess(downloadResponse);

await using (var source = await downloadResponse.Content.ReadAsStreamAsync())
await using (var destination = File.Create("pedimento-original.pdf"))
{
    await source.CopyToAsync(destination);
}

Console.WriteLine("Archivo guardado como pedimento-original.pdf");

static string RequiredEnvironment(string name)
{
    return Environment.GetEnvironmentVariable(name)
        ?? throw new InvalidOperationException(
            $"Falta la variable de ambiente {name}.");
}

static async Task<JsonElement> ReadJson(HttpResponseMessage response)
{
    var text = await response.Content.ReadAsStringAsync();
    using var document = JsonDocument.Parse(text);
    return document.RootElement.Clone();
}

static async Task EnsureSuccess(HttpResponseMessage response)
{
    if (response.IsSuccessStatusCode)
    {
        return;
    }

    var body = await response.Content.ReadAsStringAsync();
    throw new HttpRequestException(
        $"HTTP {(int)response.StatusCode} {response.ReasonPhrase}: {body}");
}

static async Task<JsonElement> WaitForResult(
    HttpClient apiClient,
    string api,
    string documentId,
    int attempts,
    TimeSpan delay)
{
    var resultUrl =
        $"{api}/documents/{Uri.EscapeDataString(documentId)}?full=1&waitSeconds=20";

    for (var attempt = 1; attempt <= attempts; attempt++)
    {
        using var response = await apiClient.GetAsync(resultUrl);

        var body = await ReadJson(response);
        // 429 "Too Many Requests" = throttle: se espera igual que un 202.
        // 429 "Limit Exceeded" (techo diario agotado) cae en EnsureSuccess.
        var throttled = response.StatusCode == HttpStatusCode.TooManyRequests
            && !(body.TryGetProperty("message", out var message)
                 && message.GetString() == "Limit Exceeded");
        if (response.StatusCode == HttpStatusCode.Accepted || throttled)
        {
            var status = throttled
                ? "429 throttle"
                : body.GetProperty("processingStatus").GetString();
            var retryAfter = response.Headers.RetryAfter?.Delta ?? delay;
            Console.WriteLine($"{status}... intento {attempt}/{attempts}");
            delay = retryAfter > delay ? retryAfter : delay;
            await Task.Delay(
                delay + TimeSpan.FromMilliseconds(Random.Shared.Next(0, 1000)));
            delay = TimeSpan.FromSeconds(Math.Min(delay.TotalSeconds * 2, 30));
            continue;
        }

        await EnsureSuccess(response);
        if (body.GetProperty("processingStatus").GetString() == "FAILED")
            throw new InvalidOperationException("El procesamiento técnico falló.");
        return body;
    }

    throw new TimeoutException(
        $"El documento {documentId} no terminó dentro del tiempo esperado.");
}

static string BuildUrl(
    string baseUrl,
    params (string Name, string Value)[] parameters)
{
    var query = string.Join(
        "&",
        parameters.Select(parameter =>
            $"{Uri.EscapeDataString(parameter.Name)}=" +
            Uri.EscapeDataString(parameter.Value)));

    return $"{baseUrl}?{query}";
}

static string Pretty(JsonElement value)
{
    return JsonSerializer.Serialize(
        value,
        new JsonSerializerOptions { WriteIndented = true });
}
~~~

Ejecuta el ejemplo pasando el PDF:

~~~bash
dotnet run -- /ruta/al/pedimento.pdf
~~~

## 3. Reprocesar un archivo ya almacenado

No necesitas descargarlo ni repetir el `PUT` a S3. El id viejo va en la ruta;
el body opcional permite cambiar tipos y cardinalidad. La respuesta trae un id
nuevo y el anterior conserva su resultado:

~~~csharp
var reprocessBody = new
{
    documentType = "PEDIMENTO_ERP:1,FACTURA_FLETE_EXTRANJERO:all",
    occurrenceMode = "multiple"
};

using var reprocessResponse = await apiClient.PostAsJsonAsync(
    $"{api}/documents/{Uri.EscapeDataString(documentId)}/reprocess",
    reprocessBody);
await EnsureSuccess(reprocessResponse);

var reprocessJob = await ReadJson(reprocessResponse);
var newDocumentId = reprocessJob.GetProperty("documentId").GetString()
    ?? throw new InvalidOperationException("El reproceso no devolvió documentId.");

var newResult = await WaitForResult(
    apiClient, api, newDocumentId, attempts: 90,
    delay: TimeSpan.FromSeconds(1));
~~~

Envía `{}` para heredar los tipos explícitos del trabajo anterior. Enviar
`documentType = (string?)null` fuerza clasificación automática.

## 4. Registrar una referencia de autenticidad

Las firmas, sellos y logos válidos de un proveedor se registran **antes** de
subir sus documentos; los tipos con verificación de autenticidad (hoy
`CARTA_NOM35`) comparan contra ellos cuando el documento llega con
`metadata.proveedor` igual a la clave registrada. Son tres peticiones, con la
misma mecánica que un documento: reservar, `PUT` a S3 y confirmar.

La app necesita el permiso de referencias — también para consultar:

~~~bash
pnpm apps:grant -- --name ERP --env prod --reference-write
~~~

El fragmento reutiliza `apiClient`, `s3Client`, `ReadJson` y `EnsureSuccess`
del programa de arriba:

~~~csharp
const string proveedor = "10040";      // el mismo valor de metadata.proveedor
const string cropPath = "firma-julio.png";   // SOLO la firma recortada

// -------------------------------------------------------------------------
// 1. Reservar. kind = FIRMA | SELLO | LOGO; label = quién firma.
// -------------------------------------------------------------------------
using var reserveResponse = await apiClient.PostAsJsonAsync(
    $"{api}/references/PROVEEDOR/{Uri.EscapeDataString(proveedor)}",
    new
    {
        kind = "FIRMA",
        label = "Lic. Julio Lemus",
        fileName = Path.GetFileName(cropPath)
    });
await EnsureSuccess(reserveResponse);

var reservation = await ReadJson(reserveResponse);
var referenceUploadUrl = reservation.GetProperty("uploadUrl").GetString()
    ?? throw new InvalidOperationException("La respuesta no contiene uploadUrl.");
// confirmUrl es una ruta relativa: /references/PROVEEDOR/10040/FIRMA/<refId>/confirm
var confirmUrl = api + reservation.GetProperty("confirmUrl").GetString();
var refId = reservation.GetProperty("refId").GetString();

// -------------------------------------------------------------------------
// 2. Subir el recorte a S3. Sin x-api-key, sin Content-Type, sin x-amz-*.
// -------------------------------------------------------------------------
await using (var crop = File.OpenRead(cropPath))
using (var cropContent = new StreamContent(crop))
using (var cropUpload = await s3Client.PutAsync(referenceUploadUrl, cropContent))
{
    await EnsureSuccess(cropUpload);
}

// -------------------------------------------------------------------------
// 3. Confirmar: revisa el recorte y responde en el momento (no hay polling).
// 422 = el recorte no pasó; 409 SIMILAR_REFERENCE = misma tinta que otra.
// -------------------------------------------------------------------------
using var confirmResponse = await apiClient.PostAsync(confirmUrl, content: null);
var confirmation = await ReadJson(confirmResponse);

if (confirmResponse.StatusCode == HttpStatusCode.UnprocessableEntity)
{
    foreach (var issue in confirmation.GetProperty("errors").EnumerateArray())
    {
        Console.WriteLine(
            $"{issue.GetProperty("code").GetString()}: " +
            issue.GetProperty("message").GetString());
    }
    throw new InvalidOperationException("El recorte no pasó la revisión.");
}

if (confirmResponse.StatusCode == HttpStatusCode.Conflict
    && confirmation.GetProperty("error").GetString() == "SIMILAR_REFERENCE")
{
    // Ya hay una muestra de esa misma firma. Para registrarla de todos modos:
    using var forced = await apiClient.PostAsJsonAsync(
        confirmUrl, new { force = true });
    await EnsureSuccess(forced);
    confirmation = await ReadJson(forced);
}
else
{
    await EnsureSuccess(confirmResponse);
}

Console.WriteLine(
    $"Referencia {refId}: {confirmation.GetProperty("status").GetString()}");

// -------------------------------------------------------------------------
// Consultar y dar de baja.
// -------------------------------------------------------------------------
var entityUrl = $"{api}/references/PROVEEDOR/{Uri.EscapeDataString(proveedor)}";

using var listResponse = await apiClient.GetAsync(
    BuildUrl(entityUrl, ("kind", "FIRMA"), ("includeInactive", "1")));
await EnsureSuccess(listResponse);
Console.WriteLine(Pretty(await ReadJson(listResponse)));

// DELETE solo desactiva: la ficha vuelve con status INACTIVE.
using var deleteResponse = await apiClient.DeleteAsync(
    $"{entityUrl}/FIRMA/{Uri.EscapeDataString(refId!)}");
await EnsureSuccess(deleteResponse);
~~~

## 5. Reglas importantes

- POST /documents manda JSON, no los bytes del archivo.
- POST /documents/{id}/reprocess crea otro id y copia el original dentro de S3;
  no devuelve uploadUrl y no cambia el resultado viejo.
- El PUT y la descarga van a S3 con un HttpClient separado y sin API key.
- La metadata es plana, usa claves en minúsculas y admite únicamente valores
  escalares US-ASCII. El mapa completo tiene un límite de 2 KB.
- app-origin y model-id son claves reservadas.
- estado_externo es un estado libre del ERP; nunca sustituye
  result.validation.status.
- Los datos extraídos viven en result.documents[], un elemento por documento
  encontrado dentro del archivo. Ese arreglo SIEMPRE está, traiga el PDF uno o
  varios: un PDF puede llegar con dos facturas y el ERP tiene que verlas las
  dos. Los campos de cada uno están en documents[i].fields, y un campo que no
  aparece significa que el documento no lo traía y no era obligatorio.
- El PATCH necesita AllowMetadataWrite=true y sigue respetando el alcance y
  los tipos autorizados de la app.
- Un 202 indica `AWAITING_UPLOAD`, `QUEUED` o `PROCESSING`; usa
  `waitSeconds=20` sobre el mismo GET, respeta `Retry-After` y aumenta la
  espera gradualmente, hasta 30 segundos.
- Un 404 significa que el documento no existe o queda fuera del alcance de la
  app. Ya no se usa para representar procesamiento pendiente.
- Las rutas /references necesitan AllowReferenceWrite=true, también los GET.
  La clave del proveedor solo admite letras, dígitos, punto, guion y guion
  bajo; si mandas otra cosa, el 400 INVALID_REFERENCE_KEY trae la forma válida
  en `suggested`. Una referencia confirmada aplica a los documentos que se
  procesen después, y registrar referencias no gasta documentos del día.

Referencia completa: [API_USO.md](../API_USO.md). Resumen:
[API_RESUMEN.md](../API_RESUMEN.md). Contrato:
[openapi.yaml](../openapi.yaml).
