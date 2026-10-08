$ErrorActionPreference = "Stop"
$image = if ($env:TRIAGE_DOCKER_IMAGE) { $env:TRIAGE_DOCKER_IMAGE } else { "triage-laudos:dev" }
$container = "triage-smoke-$([guid]::NewGuid().ToString('N'))"

docker run -d --name $container $image | Out-Null
try {
    $ready = $false
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $output = docker exec $container python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready').read().decode())" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
    }
    $ErrorActionPreference = $previousErrorActionPreference
    if (-not $ready) { throw "Container nao respondeu ao readiness." }

    $output
    docker exec $container python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready').read().decode())"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('{"instances":[{"report_text":"synthetic signal alpha"}]}'))
    docker exec $container python -c "import base64, urllib.request; request=urllib.request.Request('http://127.0.0.1:8000/v1/triage', data=base64.b64decode('$encoded'), headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(request).read().decode())"
    if ($LASTEXITCODE -ne 0) { throw "Endpoint de triagem falhou." }
} finally {
    docker rm -f $container | Out-Null
}
