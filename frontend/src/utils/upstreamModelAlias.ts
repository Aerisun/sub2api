export function isGemini38RuntimeAlias(sent: string, response: string): boolean {
  return response.trim().toLowerCase() === 'gemini-3.8-flash-n' &&
    /^gemini-3\.8-flash(?:-(?:low|medium|high|tiered))?$/.test(sent.trim().toLowerCase())
}
