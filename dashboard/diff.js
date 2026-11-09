/** Lines of a prompt for the compare view (compareLines in agents.js). Pure, no dependency. */

/** Lines as an editor shows them: a trailing newline ends the last line, it does not start another. */
export function splitLines(text) {
  if (typeof text !== 'string' || text === '') return [];
  const lines = text.split(/\r\n|\r|\n/);
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  return lines;
}
