// Gemini sometimes appends the next section title to the preceding sentence.
// Only split a complete bold title followed by a blank line. Preserve inline
// emphasis, incomplete Markdown, and fenced code while chunks are arriving.
export function formatGeminiReasoning(text: string): string {
  return text.split(/(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$))/g)
    .map((part, index) => index % 2 ? part : part.replace(
      /([.!?。！？])[ \t]*(?:\r?\n[ \t]*)?(\*\*[^*\n]{1,120}\*\*)[ \t]*(?=\r?\n[ \t]*\r?\n)/g,
      "$1\n\n$2",
    )).join("");
}
