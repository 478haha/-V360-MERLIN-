param(
    [Parameter(Mandatory = $true)]
    [string]$RuntimeDirectory
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtime = (Resolve-Path -LiteralPath $RuntimeDirectory).Path
$script = Join-Path $repo '水冷屏自定义\水冷屏控件.py'
$html = Join-Path $repo '水冷控件简版.html'
$datas = @('--add-data', "$html;.", '--add-data', "$runtime;runtime")

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name 'ValkyrieV360水冷屏控件' @datas $script
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Write-Host "Build output: $(Join-Path $repo 'dist\ValkyrieV360水冷屏控件.exe')"
