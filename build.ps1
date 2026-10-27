param(
    [Parameter(Mandatory = $true)]
    [string]$RuntimeDirectory
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$appRoot = Join-Path $repo '水冷屏自定义'
$runtime = (Resolve-Path -LiteralPath $RuntimeDirectory).Path
$script = Join-Path $appRoot '水冷屏控件.py'
$html = Join-Path $repo '水冷控件简版.html'
$dist = Join-Path $appRoot 'dist'
$work = Join-Path $appRoot 'build'
$spec = Join-Path $appRoot 'ValkyrieV360水冷屏控件.spec'
$datas = @('--add-data', "$html;.", '--add-data', "$runtime;runtime")

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name 'ValkyrieV360水冷屏控件' --distpath $dist --workpath $work `
    --specpath $appRoot @datas $script
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

Write-Host "Build output: $(Join-Path $dist 'ValkyrieV360水冷屏控件.exe')"
