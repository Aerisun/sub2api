/** Install the supported VS Code launcher after saving the two client configs. */
export function claudeStreamingSetupCommand(baseUrl: string, shell: string): string {
  const base = baseUrl.replace(/\/v1\/?$/, '').replace(/\/+$/, '')
  const url = new URL(`${base}/client-streaming/claude-stream-wrapper.cjs`)
  const singleQuote = (text: string) => `'${text.replace(/'/g, `'"'"'`)}'`
  const psQuote = (text: string) => `'${text.replace(/'/g, "''")}'`
  if (shell === 'powershell') {
    return `$streamSetup = Join-Path $env:TEMP ('aerisun-stream-' + [guid]::NewGuid() + '.cjs')
Invoke-WebRequest -Uri ${psQuote(url.href)} -OutFile $streamSetup
if (Test-Path $streamSetup) { node $streamSetup --install; Remove-Item $streamSetup }`
  }
  if (shell === 'cmd') {
    return `set "STREAM_SETUP=%TEMP%\\aerisun-stream-%RANDOM%-%RANDOM%.cjs"
curl.exe -fSL --noproxy "${url.hostname}" "${url.href}" -o "%STREAM_SETUP%"
if exist "%STREAM_SETUP%" node "%STREAM_SETUP%" --install
if exist "%STREAM_SETUP%" del "%STREAM_SETUP%"`
  }
  return `stream_setup="$(mktemp)"
if curl -fsSL --noproxy ${singleQuote(url.hostname)} ${singleQuote(url.href)} -o "$stream_setup"; then
  node "$stream_setup" --install
fi
rm -f "$stream_setup"`
}
