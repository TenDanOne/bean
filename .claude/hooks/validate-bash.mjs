// PreToolUse guard for the Bash/PowerShell tools: blocks destructive or unversioned DB commands
// against the real Supabase Postgres DB before they run. Everything else passes through.
// Adaptado de SIMU (Prisma) a Django: flush/reset_db/migrate zero en lugar de db push/migrate reset.
let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => (raw += chunk));
process.stdin.on("end", () => {
  let command = "";
  try {
    command = JSON.parse(raw)?.tool_input?.command ?? "";
  } catch {
    process.exit(0);
  }
  if (!command) process.exit(0);

  const lower = command.toLowerCase();
  let reason = "";

  if (/manage\.py\s+(flush|reset_db|sqlflush)/.test(lower)) {
    reason =
      "Bloqueado: 'manage.py flush/reset_db' borra todos los datos de la BD. Si de verdad quieres resetear, hazlo manualmente fuera de Claude Code.";
  } else if (/manage\.py\s+migrate\s+\S+\s+zero/.test(lower)) {
    reason =
      "Bloqueado: 'migrate <app> zero' revierte todas las migraciones de la app y puede destruir datos. Hazlo manualmente fuera de Claude Code.";
  } else if (/manage\.py\s+(dbshell|loaddata)[^&|;]*(supabase|:5432|:6543)/.test(lower)) {
    reason = "Bloqueado: dbshell/loaddata contra la BD real de Supabase. Hazlo manualmente fuera de Claude Code.";
  } else if (/drop\s+(table|database|schema)/.test(lower)) {
    reason =
      "Bloqueado: DROP TABLE/DATABASE/SCHEMA contra la BD real de Supabase. Si de verdad lo necesitas, hazlo manualmente fuera de Claude Code.";
  }

  if (reason) {
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: "deny",
          permissionDecisionReason: reason,
        },
      })
    );
  }
  process.exit(0);
});
