import sqlite3
import sys
import os

DB_PATH = os.getenv("SYSIBLE_DB_PATH", "/opt/sysible/backend/sysible.db")


def safe_execute(cur, query, params=()):
    try:
        cur.execute(query, params)
    except sqlite3.OperationalError as e:
        print(f"[SKIP] {e}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 unenroll_agent.py <host_id>")
        return

    host_id = sys.argv[1]

    if not os.path.exists(DB_PATH):
        print("DB not found:", DB_PATH)
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    print(f"Unenrolling agent: {host_id}")

    # =========================================================
    # CORE AGENT TABLE
    # =========================================================
    safe_execute(cur, "DELETE FROM agents WHERE host_id = ?", (host_id,))

    # =========================================================
    # TASKS (correct table name)
    # =========================================================
    safe_execute(cur, "DELETE FROM agent_tasks WHERE host_id = ?", (host_id,))

    # =========================================================
    # RESULTS (correct table name)
    # =========================================================
    safe_execute(cur, "DELETE FROM agent_results WHERE host_id = ?", (host_id,))

    conn.commit()
    conn.close()

    print("Unenroll complete.")


if __name__ == "__main__":
    main()
