#!/usr/bin/env python3
"""Fix findings and connections with NULL profile_id by setting them to the active profile."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from tools.investigation_context import get_active_profile

def main():
    profile = get_active_profile()
    profile_id = profile.name
    print(f"Active profile: {profile_id}")

    conn = sqlite3.connect("investigation.db")

    # Fix findings
    null_findings = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE profile_id IS NULL"
    ).fetchone()[0]
    if null_findings:
        conn.execute(
            "UPDATE findings SET profile_id = ? WHERE profile_id IS NULL",
            (profile_id,),
        )
        print(f"Updated {null_findings} findings with profile_id = '{profile_id}'")

    # Fix connections — delete duplicates first, then update remaining
    null_connections = conn.execute(
        "SELECT COUNT(*) FROM connections WHERE profile_id IS NULL"
    ).fetchone()[0]
    if null_connections:
        # Delete NULL-profile connections that duplicate existing profiled ones
        dupes = conn.execute(
            """DELETE FROM connections WHERE profile_id IS NULL
               AND id IN (
                   SELECT c1.id FROM connections c1
                   INNER JOIN connections c2
                   ON c1.person_a = c2.person_a
                   AND c1.person_b = c2.person_b
                   AND c1.relationship_type = c2.relationship_type
                   AND c2.profile_id = ?
                   WHERE c1.profile_id IS NULL
               )""",
            (profile_id,),
        )
        if dupes.rowcount:
            print(f"Deleted {dupes.rowcount} duplicate NULL connections")

        # Update remaining
        remaining = conn.execute(
            "UPDATE connections SET profile_id = ? WHERE profile_id IS NULL",
            (profile_id,),
        )
        print(f"Updated {remaining.rowcount} connections with profile_id = '{profile_id}'")

    conn.commit()
    conn.close()

    if not null_findings and not null_connections:
        print("No NULL profile_id records found.")

if __name__ == "__main__":
    main()
