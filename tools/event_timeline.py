#!/usr/bin/env python3
"""
External event timeline for temporal correlation in OSINT investigations.

Maintains a table of key external events (arrests, lawsuits, elections, deaths, etc.)
for correlation with investigation findings. Seeded with ~100+ key dates.

Part of investigation.db.

Usage:
    python tools/event_timeline.py seed                                     # populate key dates
    python tools/event_timeline.py add --date 2019-07-06 --name "Epstein arrested" --category arrest
    python tools/event_timeline.py window --start 2019-07-01 --end 2019-07-15
    python tools/event_timeline.py near --finding-id 412 --days 14
    python tools/event_timeline.py near --date 2019-03-08 --days 7
    python tools/event_timeline.py list [--category legal] [--year 2019]
    python tools/event_timeline.py stats
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

try:
    from tools.output_util import add_output_args, write_output
except ImportError:
    from output_util import add_output_args, write_output

try:
    from tools.lead_tracker import get_db
except ImportError:
    from lead_tracker import get_db

VALID_CATEGORIES = [
    "legal", "political", "financial", "media", "death", "arrest",
    "election", "regulatory", "corporate", "intelligence", "other"
]


# ── Schema ────────────────────────────────────────────────────

def _ensure_timeline_schema(db):
    """Create event_timeline table if it doesn't exist."""
    db.executescript("""
        CREATE TABLE IF NOT EXISTS event_timeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date DATE NOT NULL,
            event_name TEXT NOT NULL,
            category TEXT CHECK(category IN (
                'legal','political','financial','media','death','arrest',
                'election','regulatory','corporate','intelligence','other'
            )),
            description TEXT,
            relevance TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(event_date, event_name)
        );

        CREATE INDEX IF NOT EXISTS idx_events_date ON event_timeline(event_date);
        CREATE INDEX IF NOT EXISTS idx_events_category ON event_timeline(category);
    """)


def get_timeline_db():
    """Get DB connection with timeline schema ensured."""
    db = get_db()
    _ensure_timeline_schema(db)
    return db


# ── Seed Data ────────────────────────────────────────────────────

SEED_EVENTS = [
    # Epstein legal timeline
    ("2005-03-15", "Palm Beach PD investigation begins", "legal",
     "Palm Beach Police receive complaint from parent of 14-year-old", "Start of first investigation"),
    ("2006-05-01", "FBI opens federal investigation", "legal",
     "FBI begins Operation Leap Year investigation of Epstein", "Federal involvement escalates case"),
    ("2007-06-30", "Non-Prosecution Agreement drafted", "legal",
     "NPA between SDFL and Epstein's lawyers (Starr, Dershowitz, Lefkowitz)", "Key plea deal"),
    ("2008-06-30", "Epstein pleads guilty FL state charges", "legal",
     "Pleads guilty to solicitation of prostitution, 18 months county jail", "State plea deal"),
    ("2008-07-01", "Epstein begins jail sentence", "arrest",
     "Reports to Palm Beach County Stockade; gets work release program", "Lenient sentencing"),
    ("2009-07-22", "Epstein released from jail", "legal",
     "Released after 13 months; begins 12-month probation with sex offender registration", "End of FL sentence"),
    ("2009-01-01", "Courtney Wild civil lawsuit filed", "legal",
     "Jane Doe 1 and Jane Doe 2 file Crime Victims' Rights Act lawsuit", "Challenges NPA"),
    ("2010-03-22", "Virginia Giuffre joins CVRA case", "legal",
     "Giuffre (then Roberts) joins as Jane Doe 3 in Edwards v. USA", "Key victim enters case"),
    ("2015-01-05", "Giuffre v. Maxwell filed", "legal",
     "Defamation suit filed SDNY (15-cv-7433) after Maxwell called allegations lies", "Major civil case"),
    ("2016-04-07", "Giuffre v. Maxwell depositions begin", "legal",
     "Key depositions of Giuffre and Maxwell taken", "Critical testimony phase"),
    ("2017-05-25", "Giuffre v. Maxwell settled", "legal",
     "Settled for undisclosed amount; documents sealed", "Settlement but documents become key"),
    ("2018-11-28", "Miami Herald 'Perversion of Justice' published", "media",
     "Julie K. Brown's investigative series exposing NPA details", "Revived public interest and legal action"),
    ("2019-02-21", "Judge rules NPA violated CVRA", "legal",
     "Judge Marra finds prosecutors violated Crime Victims' Rights Act", "Legal vindication of victims"),
    ("2019-03-08", "Epstein obtains new passport", "other",
     "New passport issued; previously reported old one as lost", "Pre-arrest activity"),
    ("2019-07-06", "Epstein arrested at Teterboro Airport", "arrest",
     "SDNY agents arrest Epstein on sex trafficking charges returning from Paris", "Second federal arrest"),
    ("2019-07-08", "SDNY indictment unsealed", "legal",
     "Indictment charges sex trafficking and conspiracy (2002-2005)", "Federal charges"),
    ("2019-07-18", "Bail denied", "legal",
     "Judge Berman denies bail despite $100M+ offer; flight risk", "Remains in MCC"),
    ("2019-07-23", "First apparent suicide attempt at MCC", "other",
     "Found semi-conscious with marks on neck; cellmate Nicholas Tartaglione present", "First incident"),
    ("2019-08-08", "1953 Trust renamed to 1953 Trust", "financial",
     "Trust renamed 2 days before death; Indyke/Kahn made sole trustees", "Pre-death financial moves"),
    ("2019-08-09", "Giuffre v. Maxwell documents unsealed", "legal",
     "2,000+ pages of depositions and discovery released by court order", "Major document release"),
    ("2019-08-10", "Epstein found dead at MCC", "death",
     "Found unresponsive in cell at Metropolitan Correctional Center NYC", "Death in custody"),
    ("2019-08-12", "Zorro Ranch LLC agent resigned", "corporate",
     "NM registered agent resigned; entity revoked 5 weeks post-arrest", "Post-death corporate dissolution"),
    ("2019-08-19", "SDNY charges formally dismissed", "legal",
     "Charges dismissed due to death; but investigation into co-conspirators continues", "Case ended for Epstein"),
    ("2019-10-29", "AG Barr announces MCC investigation results", "legal",
     "Claims 'perfect storm of screw-ups' not foul play; cameras malfunctioned", "Official explanation"),
    ("2019-12-09", "Deutsche Bank fined $150M", "regulatory",
     "NYDFS consent order for DB compliance failures including Epstein accounts", "Regulatory action"),

    # Maxwell timeline
    ("2020-07-02", "Ghislaine Maxwell arrested", "arrest",
     "FBI arrests Maxwell at NH property; charged with conspiracy and sex trafficking", "Key co-conspirator"),
    ("2020-10-22", "Maxwell documents further unsealed", "legal",
     "Additional Giuffre v. Maxwell depositions released", "More document releases"),
    ("2021-11-29", "Maxwell trial begins", "legal",
     "Trial begins in SDNY before Judge Nathan", "Major trial"),
    ("2021-12-29", "Maxwell convicted on 5 of 6 counts", "legal",
     "Guilty of sex trafficking, conspiracy, transporting minor", "Conviction"),
    ("2022-06-28", "Maxwell sentenced to 20 years", "legal",
     "Sentenced by Judge Nathan; victims testified at sentencing", "Sentencing"),

    # Related deaths
    ("2017-02-20", "Vitaly Churkin dies suddenly", "death",
     "Russian UN Ambassador dies of heart attack day before 65th birthday", "Suspicious timing in network"),
    ("2018-10-02", "Jamal Khashoggi killed", "death",
     "Saudi journalist killed at Istanbul consulate by MBS team", "Gulf state violence"),
    ("2019-01-26", "Mark Middleton found dead", "death",
     "Former Clinton WH aide found hanged at Heifer International ranch", "Connected to Clinton-Epstein access"),

    # Political/election events
    ("2016-11-08", "Trump elected President", "election",
     "Trump wins presidential election", "Major political shift — many Epstein associates in orbit"),
    ("2017-01-20", "Trump inaugurated", "political",
     "45th President takes office", "Transition of power"),
    ("2017-05-17", "Mueller appointed Special Counsel", "political",
     "Rod Rosenstein appoints Mueller to investigate Russia interference", "Political context"),
    ("2018-11-06", "2018 midterm elections", "election",
     "Democrats take House; Stacey Plaskett wins USVI delegate", "Plaskett received Epstein-linked FEC donations"),
    ("2019-04-18", "Mueller Report released", "political",
     "Special Counsel report made public", "Political context"),
    ("2020-11-03", "Biden elected President", "election",
     "Biden wins presidential election", "Political transition"),

    # Financial events
    ("1988-01-01", "Epstein starts at J. Epstein & Co", "financial",
     "Leaves Bear Stearns; incorporates J. Epstein & Co (Gruss & Wilner = same firm)", "Financial independence"),
    ("1996-01-01", "Financial Trust Company established USVI", "financial",
     "FTC established as Wexner family trust structure in USVI", "Key trust infrastructure"),
    ("2003-09-01", "Southern Trust Company formed", "financial",
     "STC formed in USVI; will become primary Epstein vehicle", "Key financial entity created"),
    ("2008-01-01", "Deutsche Bank opens Epstein accounts", "financial",
     "DB Private Bank opens accounts despite sex offender status", "DB relationship begins"),
    ("2013-01-01", "STC receives $40M from Black in one year", "financial",
     "Leon Black transfers $15M + $16.5M + $8.5M to STC", "Peak Black-Epstein financial flows"),
    ("2015-12-01", "STC balance peaks at ~$110M", "financial",
     "Southern Trust Company reaches maximum balance", "Peak financial position"),
    ("2018-09-01", "Deutsche Bank closes Epstein accounts", "financial",
     "DB terminates relationship after internal compliance review", "DB exits"),
    ("2019-04-01", "JPMorgan closes Epstein accounts", "financial",
     "JPM terminates relationship approximately April 2019", "JPM exits"),
    ("2019-08-08", "1953 Trust restructured", "financial",
     "Pour-over will executed; 1953 Trust renamed; assets consolidated", "Pre-death financial planning"),
    ("2019-12-31", "STC IB consolidation year-end", "financial",
     "STC consolidated as investment business entity per DB records", "Post-death financial close"),

    # Regulatory actions
    ("2007-09-01", "SEC Epstein investigation closed", "regulatory",
     "SEC investigation into Epstein's financial dealings closed without action", "Regulatory failure"),
    ("2020-01-09", "NYDFS consent order vs Deutsche Bank", "regulatory",
     "NY DFS fines DB $150M for Epstein-related compliance failures", "Regulatory consequence"),
    ("2020-07-01", "JPMorgan USVI lawsuit filed", "legal",
     "USVI AG files suit against JPMorgan for facilitating Epstein trafficking", "Major financial lawsuit"),
    ("2022-11-21", "USVI v. JPMorgan (SDNY transferred)", "legal",
     "Case 1:22-cv-10904 filed/transferred to SDNY", "Federal financial case"),
    ("2023-06-01", "JPMorgan settles for $290M", "financial",
     "JPM settles USVI lawsuit; also settles Staley-related claims", "Major financial settlement"),
    ("2023-11-01", "Deutsche Bank settles for $75M", "financial",
     "DB settles with Epstein victims in class action", "DB financial consequence"),

    # Key media/disclosure events
    ("2006-02-01", "Palm Beach PD probable cause affidavit", "legal",
     "Detailed affidavit establishes pattern with multiple victims", "Key early document"),
    ("2010-03-01", "Epstein science philanthropy era begins", "financial",
     "Post-release focus on science funding: Edge, Harvard, MIT", "Reputation rehabilitation"),
    ("2011-03-01", "Vanity Fair 'The Talented Mr. Epstein'", "media",
     "Wolff profile on Epstein's post-prison social rehabilitation", "Media complicity"),
    ("2015-01-06", "Prince Andrew allegations made public", "media",
     "Giuffre allegations against Prince Andrew enter public record", "International dimension"),
    ("2019-08-10", "Cameras in MCC SHU malfunction", "other",
     "Two cameras outside Epstein's cell found to have 'malfunctioned'", "Suspicious circumstances"),
    ("2019-10-01", "Hard drives replaced at MCC same night", "other",
     "Server room hard drives reportedly replaced night of death", "Evidence concerns"),
    ("2020-01-01", "FOIA requests begin yielding documents", "media",
     "First wave of FOIA documents from FBI, DOJ, BOP released", "Document releases begin"),
    ("2023-01-01", "Epstein documents unsealed (2023 batch)", "legal",
     "Court orders unsealing of additional Giuffre v. Maxwell documents", "Major disclosure"),
    ("2024-01-04", "Epstein associate list partially released", "legal",
     "Court releases additional names from sealed Giuffre v. Maxwell docs", "Major disclosure event"),

    # Corporate events
    ("1991-01-01", "Wexner grants POA to Epstein", "corporate",
     "Limited Brands founder grants power of attorney to Epstein", "Key corporate control"),
    ("1996-01-01", "9 E 71st St transferred to Epstein", "corporate",
     "Wexner transfers NYC mansion to Epstein-controlled entity for $0", "Major real estate transfer"),
    ("2003-07-01", "Epstein incorporates multiple USVI entities", "corporate",
     "Wave of USVI entity formations including STC and related vehicles", "Corporate infrastructure"),
    ("2007-10-01", "Wexner formally terminates Epstein relationship", "corporate",
     "Public break; claims Epstein 'misappropriated' funds", "Official separation"),
    ("2011-01-01", "Leon Black begins consulting payments to STC", "corporate",
     "First known payments from Black to Epstein via STC", "Black-STC relationship starts"),
    ("2019-08-14", "Indyke/Kahn appointed estate executors", "corporate",
     "Epstein's attorneys Darren Indyke and Richard Kahn named co-executors", "Estate control"),
    ("2021-03-22", "Leon Black resigns as Apollo CEO", "corporate",
     "Resigns citing 'unfair' media scrutiny of Epstein relationship", "Apollo fallout"),

    # Intelligence-related
    ("1985-01-01", "Robert Maxwell acquires Mirror Group", "intelligence",
     "Ghislaine's father becomes major UK media mogul", "Maxwell media empire"),
    ("1991-11-05", "Robert Maxwell dies at sea", "death",
     "Found floating near yacht Lady Ghislaine off Canary Islands", "Patriarch death; alleged Mossad ties"),
    ("2009-01-01", "Ehud Barak begins Epstein relationship", "intelligence",
     "Former Israeli PM starts receiving Epstein funding; later Carbyne", "Israeli intelligence nexus"),
    ("2016-01-01", "Carbyne911 founded", "corporate",
     "Emergency tech company co-founded by Barak with Epstein investment", "Israeli tech venture"),
    ("2019-06-01", "Barak photographed entering Epstein NYC residence", "media",
     "Daily Mail publishes photos of Barak entering 9 E 71st St", "Israeli nexus exposure"),

    # Gulf state events
    ("2016-01-01", "Qatar diplomatic crisis begins escalating", "political",
     "Regional tensions building toward 2017 blockade", "Gulf operations context"),
    ("2017-06-05", "Saudi-UAE-Bahrain-Egypt blockade Qatar", "political",
     "Major diplomatic crisis; Epstein network active on both sides", "Gulf crisis"),
    ("2018-01-01", "Broidy-Nader lobbying operation exposed", "political",
     "Anti-Qatar lobbying connected to both Broidy and Nader, with links to Epstein orbit",
     "Gulf lobbying nexus"),
    ("2018-04-06", "George Nader arrested on child porn charges", "arrest",
     "Key Gulf intermediary arrested at Dulles Airport", "Gulf-Epstein orbit intersection"),

    # CBP/Travel
    ("2005-10-01", "CBP TECS lookout placed on Epstein", "intelligence",
     "Customs and Border Protection places enhanced screening on Epstein travel", "Travel monitoring begins"),
    ("2016-12-01", "CBP enhanced screening escalated", "intelligence",
     "Travel screening intensified in late 2016", "Increased surveillance"),

    # Apollo/Financial
    ("2021-01-25", "Dechert report on Black-Epstein relationship", "corporate",
     "Internal review finds $158M+ in payments; report later destroyed", "Key financial review"),
    ("2021-01-01", "Marc Rowan becomes Apollo CEO", "corporate",
     "Rowan succeeds Black; later found to have own Epstein connections", "Apollo succession"),
    ("2022-03-01", "Josh Harris buys Washington Commanders", "financial",
     "Apollo co-founder Harris leads NFL team purchase; Epstein connections surface", "Harris exposure"),

    # Additional key dates
    ("1992-01-01", "Ghislaine Maxwell moves to New York", "other",
     "After father's death, relocates to NYC; relationship with Epstein begins", "Maxwell-Epstein nexus begins"),
    ("2002-03-01", "FBI first tips about Epstein", "intelligence",
     "FBI receives first intelligence tips about Epstein and underage girls", "Early FBI awareness"),
    ("2004-01-01", "Epstein flies Clinton on Lolita Express", "political",
     "Multiple Clinton flights on Epstein aircraft documented in FAA logs", "Political connections"),
    ("2006-03-01", "Ken Starr retained by Epstein defense", "legal",
     "Former Independent Counsel joins Epstein legal team with Dershowitz", "Elite legal defense"),
    ("2008-12-01", "Bear Stearns collapses / JPM acquires", "financial",
     "Bear Stearns (Epstein's former employer) fails; JPM absorbs", "Financial crisis context"),
    ("2010-06-01", "Bill Gates first meets Epstein", "other",
     "Gates visits Epstein NYC mansion; relationship continues to 2014", "Tech billionaire connection"),
    ("2014-01-01", "Gates-Epstein meetings continue", "other",
     "Multiple meetings; Gates aide Boris Nikolic serves as Epstein network broker", "Ongoing tech nexus"),
    ("2019-09-06", "MIT Media Lab Epstein funding exposed", "media",
     "New Yorker reveals Joi Ito solicited Epstein donations for MIT", "Academic funding exposure"),
    ("2019-09-18", "Joi Ito resigns from MIT", "corporate",
     "MIT Media Lab director resigns after Epstein funding revelations", "Academic consequences"),
    ("2019-10-12", "Leon Black donations to Harvard exposed", "media",
     "Black's $50M+ Harvard donations scrutinized for Epstein connection", "Academic funding scrutiny"),
    ("2020-08-01", "House Oversight 20K documents released", "legal",
     "House Oversight Committee releases ~20,000 Epstein-related documents", "Major document release"),
    ("2023-06-22", "Jes Staley charged by FCA", "regulatory",
     "Former JPMorgan CEO charged by UK FCA for misleading regulators about Epstein", "Regulatory consequence"),
    ("2023-09-01", "Virgin Islands v. Epstein Estate settled", "legal",
     "USVI settles with Epstein Estate for $105M; includes Little St James", "Estate settlement"),
]


# ── CRUD ────────────────────────────────────────────────────

def seed_events():
    """Populate event_timeline from active investigation profile's key_dates.

    Falls back to legacy SEED_EVENTS list if no profile is active or
    if the profile has no key_dates defined.
    """
    db = get_timeline_db()
    added = 0
    skipped = 0

    # Try loading from active investigation profile first
    profile_events = []
    try:
        from tools.investigation_context import get_active_profile
        profile = get_active_profile()
        if profile and profile.key_dates:
            for kd in profile.key_dates:
                profile_events.append((
                    kd.get("date", ""),
                    kd.get("event", ""),
                    kd.get("category", "other"),
                    kd.get("event", ""),  # description = event text
                    None,  # relevance
                ))
            print(f"Loading {len(profile_events)} key dates from profile '{profile.name}'")
    except Exception:
        pass

    events_to_seed = profile_events if profile_events else SEED_EVENTS
    source_label = "profile-seed" if profile_events else "seed"

    for event_date, event_name, category, description, relevance in events_to_seed:
        try:
            db.execute("""
                INSERT INTO event_timeline (event_date, event_name, category, description, relevance, source)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (event_date, event_name, category, description, relevance, source_label))
            added += 1
        except sqlite3.IntegrityError:
            skipped += 1
    db.commit()
    db.close()
    return added, skipped


def add_event(event_date, event_name, category, description=None, relevance=None, source=None):
    """Add a single event."""
    if category not in VALID_CATEGORIES:
        print(f"ERROR: Invalid category '{category}'. Valid: {VALID_CATEGORIES}")
        return None

    db = get_timeline_db()
    try:
        cursor = db.execute("""
            INSERT INTO event_timeline (event_date, event_name, category, description, relevance, source)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (event_date, event_name, category, description, relevance, source))
        event_id = cursor.lastrowid
        db.commit()
        db.close()
        return event_id
    except sqlite3.IntegrityError:
        db.close()
        print(f"ERROR: Event already exists for {event_date}: {event_name}")
        return None


def list_events(category=None, year=None, limit=100):
    """List events with optional filters."""
    db = get_timeline_db()
    conditions = []
    params = []

    if category:
        conditions.append("category = ?")
        params.append(category)
    if year:
        conditions.append("event_date LIKE ?")
        params.append(f"{year}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT * FROM event_timeline {where} ORDER BY event_date LIMIT ?"
    params.append(limit)
    rows = db.execute(query, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def window_events(start_date, end_date):
    """Get events and findings in a date range."""
    db = get_timeline_db()

    events = [dict(r) for r in db.execute("""
        SELECT * FROM event_timeline
        WHERE event_date BETWEEN ? AND ?
        ORDER BY event_date
    """, (start_date, end_date)).fetchall()]

    # Also find findings with dates in this range
    findings = [dict(r) for r in db.execute("""
        SELECT id, target_name, summary, confidence, date_of_event, created_at
        FROM findings
        WHERE date_of_event BETWEEN ? AND ?
        ORDER BY date_of_event
    """, (start_date, end_date)).fetchall()]

    db.close()
    return {"events": events, "findings": findings,
            "start": start_date, "end": end_date}


def near_finding(finding_id, days=14):
    """Find events within N days of a finding's date_of_event or created_at."""
    db = get_timeline_db()

    finding = db.execute(
        "SELECT id, target_name, summary, date_of_event, created_at FROM findings WHERE id = ?",
        (finding_id,)
    ).fetchone()
    if not finding:
        db.close()
        return None

    ref_date = finding["date_of_event"] or finding["created_at"][:10]

    events = [dict(r) for r in db.execute("""
        SELECT * FROM event_timeline
        WHERE event_date BETWEEN date(?, ?) AND date(?, ?)
        ORDER BY event_date
    """, (ref_date, f"-{days} days", ref_date, f"+{days} days")).fetchall()]

    db.close()
    return {
        "finding": dict(finding),
        "reference_date": ref_date,
        "days": days,
        "events": events
    }


def near_date(date_str, days=7):
    """Find events within N days of a given date."""
    db = get_timeline_db()
    events = [dict(r) for r in db.execute("""
        SELECT * FROM event_timeline
        WHERE event_date BETWEEN date(?, ?) AND date(?, ?)
        ORDER BY event_date
    """, (date_str, f"-{days} days", date_str, f"+{days} days")).fetchall()]
    db.close()
    return {"reference_date": date_str, "days": days, "events": events}


def timeline_stats():
    """Get timeline statistics."""
    db = get_timeline_db()
    stats = {}

    stats["total"] = db.execute("SELECT COUNT(*) as n FROM event_timeline").fetchone()["n"]

    # By category
    by_cat = {}
    for row in db.execute(
        "SELECT category, COUNT(*) as n FROM event_timeline GROUP BY category ORDER BY n DESC"
    ):
        by_cat[row["category"]] = row["n"]
    stats["by_category"] = by_cat

    # Date range
    row = db.execute(
        "SELECT MIN(event_date) as earliest, MAX(event_date) as latest FROM event_timeline"
    ).fetchone()
    stats["earliest"] = row["earliest"]
    stats["latest"] = row["latest"]

    # By decade
    by_decade = {}
    for row in db.execute("""
        SELECT (CAST(substr(event_date, 1, 3) AS INTEGER) * 10) as decade,
               COUNT(*) as n
        FROM event_timeline GROUP BY decade ORDER BY decade
    """):
        by_decade[f"{row['decade']}s"] = row["n"]
    stats["by_decade"] = by_decade

    db.close()
    return stats


# ── CLI ────────────────────────────────────────────────────

def _format_event(e, verbose=False):
    """Format an event for display."""
    cat = f"[{e['category']:<12}]" if e.get("category") else ""
    line = f"  {e['event_date']}  {cat}  {e['event_name']}"
    if verbose and e.get("description"):
        line += f"\n                              {e['description'][:200]}"
    if verbose and e.get("relevance"):
        line += f"\n                              -> {e['relevance'][:150]}"
    return line


def main():
    parser = argparse.ArgumentParser(description="External event timeline for temporal correlation")
    sub = parser.add_subparsers(dest="command")

    # seed
    sub.add_parser("seed", help="Populate ~100+ key dates")

    # add
    p_add = sub.add_parser("add", help="Add an event")
    p_add.add_argument("--date", required=True, help="YYYY-MM-DD")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--category", required=True, choices=VALID_CATEGORIES)
    p_add.add_argument("--description")
    p_add.add_argument("--relevance")
    p_add.add_argument("--source")

    # window
    p_win = sub.add_parser("window", help="Events and findings in a date range")
    p_win.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_win.add_argument("--end", required=True, help="YYYY-MM-DD")
    add_output_args(p_win)

    # near
    p_near = sub.add_parser("near", help="Events near a finding or date")
    p_near.add_argument("--finding-id", type=int)
    p_near.add_argument("--date")
    p_near.add_argument("--days", type=int, default=14)
    add_output_args(p_near)

    # list
    p_list = sub.add_parser("list", help="List events")
    p_list.add_argument("--category", choices=VALID_CATEGORIES)
    p_list.add_argument("--year", type=int)
    p_list.add_argument("--limit", type=int, default=100)
    p_list.add_argument("-v", "--verbose", action="store_true")
    add_output_args(p_list)

    # stats
    sub.add_parser("stats", help="Timeline statistics")

    args = parser.parse_args()

    if args.command == "seed":
        added, skipped = seed_events()
        print(f"Seeded {added} events ({skipped} already existed)")

    elif args.command == "add":
        event_id = add_event(args.date, args.name, args.category,
                             args.description, args.relevance, args.source)
        if event_id:
            print(f"Event #{event_id} created: {args.date} {args.name}")

    elif args.command == "window":
        result = window_events(args.start, args.end)
        if write_output(result, args, summary=f"window {args.start} to {args.end}"):
            return
        events = result["events"]
        findings = result["findings"]
        print(f"Window: {args.start} to {args.end}")
        print(f"  {len(events)} events, {len(findings)} findings\n")
        if events:
            print("Events:")
            for e in events:
                print(_format_event(e, verbose=True))
        if findings:
            print("\nFindings with dates in range:")
            for f in findings:
                print(f"  {f['date_of_event']}  [{f['target_name']}] {f['summary'][:80]}")

    elif args.command == "near":
        if args.finding_id:
            result = near_finding(args.finding_id, args.days)
            if not result:
                print(f"Finding #{args.finding_id} not found")
                sys.exit(1)
            if write_output(result, args, summary=f"near finding #{args.finding_id}"):
                return
            f = result["finding"]
            print(f"Events within {args.days} days of finding #{f['id']} "
                  f"({f['target_name']}, ref date: {result['reference_date']}):")
            if not result["events"]:
                print("  No events found in range")
            for e in result["events"]:
                print(_format_event(e, verbose=True))
        elif args.date:
            result = near_date(args.date, args.days)
            if write_output(result, args, summary=f"near {args.date}"):
                return
            print(f"Events within {args.days} days of {args.date}:")
            if not result["events"]:
                print("  No events found in range")
            for e in result["events"]:
                print(_format_event(e, verbose=True))
        else:
            print("ERROR: Provide --finding-id or --date")

    elif args.command == "list":
        results = list_events(category=args.category, year=args.year, limit=args.limit)
        if write_output(results, args, summary=f"events ({len(results)})"):
            return
        if not results:
            print("No events found.")
            return
        print(f"Events ({len(results)}):")
        for e in results:
            print(_format_event(e, verbose=args.verbose))

    elif args.command == "stats":
        s = timeline_stats()
        print("Event Timeline Statistics")
        print("=" * 40)
        print(f"  Total events:  {s['total']}")
        print(f"  Date range:    {s['earliest']} to {s['latest']}")
        if s["by_category"]:
            print(f"\nBy category:")
            for cat, n in s["by_category"].items():
                print(f"  {cat:<14} {n}")
        if s["by_decade"]:
            print(f"\nBy decade:")
            for dec, n in s["by_decade"].items():
                print(f"  {dec:<8} {n}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
