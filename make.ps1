<#
.SYNOPSIS
    Makefile shim for Windows, where `make` is usually not installed.

.DESCRIPTION
    Mirrors the targets in the Makefile so the documented commands work on a
    stock Windows machine with no extra tooling:

        .\make.ps1 help
        .\make.ps1 pipeline
        .\make.ps1 ask "top 5 products by profit"
        .\make.ps1 build marts

    The second argument maps to whatever the target takes - a question for `ask`,
    a dbt selector for `build`, a table list for `ingest`.
#>
param(
    [Parameter(Position = 0)][string]$Target = "help",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest
)

# Deliberately NOT "Stop": many of the tools invoked here (docker, prefect, dbt)
# write progress and warnings to stderr, and PowerShell 5.1 turns native stderr
# into a terminating NativeCommandError under "Stop". Exit codes are the signal
# that matters, and they are propagated at the end of the script.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

# Prefer the project virtualenv so no activation step is ever needed.
$py = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$dbt = Join-Path $root "venv\Scripts\dbt.exe"
if (-not (Test-Path $dbt)) { $dbt = "dbt" }

$flow = "orchestration\prefect\flows\retail_pipeline.py"
$extra = if ($Rest) { ($Rest -join " ").Trim() } else { "" }

function Invoke-Dbt([string[]]$dbtArgs) {
    Push-Location (Join-Path $root "dbt")
    try { & $dbt @dbtArgs --profiles-dir . } finally { Pop-Location }
}

switch ($Target.ToLower()) {
    "help" {
        Write-Host "Retail Sales Intelligence Platform" -ForegroundColor Cyan
        Write-Host ""
        @(
            @("setup",    "Install dependencies and dbt packages"),
            @("up",       "Start MinIO, Prefect and the dbt docs site"),
            @("down",     "Stop all containers"),
            @("status",   "Show container and warehouse status"),
            @("ingest",   "Extract SQL Server -> MinIO Bronze"),
            @("build",    "Build Silver + Gold + analytics, run all tests"),
            @("test",     "Run the dbt data tests only"),
            @("docs",     "Regenerate the dbt lineage site"),
            @("snapshot", "Publish the serving snapshot"),
            @("pipeline", "Full run: ingest -> build -> snapshot -> docs"),
            @("refresh",  "Rebuild from Bronze already in MinIO"),
            @("serve",    "Expose the flow in the Prefect UI"),
            @("ask",      "Ask a question"),
            @("shell",    "Interactive question shell"),
            @("api",      "Start the HTTP API on :8000"),
            @("eval",     "Measure text-to-SQL accuracy"),
            @("schema",   "Print the schema context sent to the model"),
            @("clean",    "Remove build artefacts and the local warehouse")
        ) | ForEach-Object { "  {0,-10} {1}" -f $_[0], $_[1] | Write-Host }
        Write-Host ""
        Write-Host "  Examples:" -ForegroundColor DarkGray
        Write-Host '    .\make.ps1 ask "top 5 products by profit"' -ForegroundColor DarkGray
        Write-Host '    .\make.ps1 build marts' -ForegroundColor DarkGray
    }
    "setup" {
        & $py -m pip install -r requirements.txt
        Invoke-Dbt @("deps")
        New-Item -ItemType Directory -Force -Path "duckdb_warehouse", "dbt\target" | Out-Null
        Write-Host "Setup complete. Copy .env.example to .env and fill in the OLTP_* values." -ForegroundColor Green
    }
    "up" {
        docker compose up -d
        Write-Host "MinIO     http://localhost:9002  (minioadmin / minioadminpassword)"
        Write-Host "Prefect   http://localhost:4200"
        Write-Host "dbt docs  http://localhost:8081"
    }
    "down"     { docker compose down }
    "logs"     { docker compose logs -f --tail=50 }
    "status"   {
        docker compose ps 2>$null
        & $py -c "import duckdb,os; p='duckdb_warehouse/warehouse.duckdb'; print('warehouse:', 'missing - run pipeline' if not os.path.exists(p) else str(duckdb.connect(p, read_only=True).execute('select count(*) from xom_retails_gold.fact_sales').fetchone()[0]) + ' fact rows')"
    }
    "ingest"   { if ($extra) { & $py "Ingestion\ingestion.py" --tables $Rest } else { & $py "Ingestion\ingestion.py" } }
    "build"    { if ($extra) { Invoke-Dbt @("build", "--select", $extra) } else { Invoke-Dbt @("build") } }
    "test"     { Invoke-Dbt @("test") }
    "docs"     { Invoke-Dbt @("docs", "generate") }
    "snapshot" { & $py $flow --no-ingest --no-transform --no-docs }
    "pipeline" { & $py $flow }
    "refresh"  { & $py $flow --no-ingest }
    "serve"    { & $py $flow --serve }
    "ask"      {
        if (-not $extra) { Write-Host 'Usage: .\make.ps1 ask "your question"' -ForegroundColor Yellow; exit 1 }
        & $py -m text2sql $extra
    }
    "shell"    { & $py -m text2sql }
    "api"      { & $py -m uvicorn "api.main:app" --reload }
    "eval"     { if ($extra) { & $py -m text2sql.eval.run_eval --provider $extra } else { & $py -m text2sql.eval.run_eval } }
    "schema"   { & $py -m text2sql --schema }
    "clean"    {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "dbt\target\*", "dbt\logs", "duckdb_warehouse\*.duckdb", "duckdb_warehouse\*.wal"
        Write-Host "Removed build artefacts. Run .\make.ps1 pipeline to rebuild." -ForegroundColor Green
    }
    default {
        Write-Host "Unknown target '$Target'. Run .\make.ps1 help" -ForegroundColor Red
        exit 1
    }
}

# Propagate the real exit code from whichever tool ran, so CI and && chains work.
if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
