#!/usr/bin/env python3
"""
One-time migration: Parse Outstanding Actions from epstein-email-osint.md into investigation.db leads.

Also seeds high-priority leads from confirmed intelligence findings.
"""

import sys
from pathlib import Path

# Add parent to path so we can import lead_tracker
sys.path.insert(0, str(Path(__file__).parent.parent))
from tools.lead_tracker import add_lead, get_db

# ── Outstanding Actions (parsed from epstein-email-osint.md) ──────────
# Only open (unchecked) items. Completed items are skipped.

HIGH_PRIORITY = [
    {
        "title": "Run GHunt on Epstein emails after interactive auth",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "GHunt requires interactive auth setup. Run ghunt login manually first, then scan known Gmail addresses.",
    },
    {
        "title": "Run Dehashed priority queue (breach database searches)",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Multiple Dehashed searches pending. Requires API key. See Updated Dehashed Priority Queue in master research file.",
    },
    {
        "title": "Investigate @gie1114 Twitter suspension — who registered this?",
        "category": "digital",
        "target_name": "Gie Marinese",
        "description": "Gie Marinese died 2000, Twitter launched 2006. Account suspended. TikTok active with display name 'Gie'. Duolingo joined Jan 2026. Likely family member using username.",
    },
    {
        "title": "Dehashed search for username sultan175",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Match.com username sultan175. 27 Sherlock hits all ruled out as different people. Check breach databases for associated emails.",
    },
    {
        "title": "Crack Gravatar MD5 hash 1537bf4e967f3e86bafd64df32da4c4d",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Gravatar-specific password hash. Try hashcat/JtR with known password patterns (jeevacation12, trd207, 800128, 1Island).",
    },
    {
        "title": "Crack Myspace SHA-1 hashes for jeffreyepsteinorg@gmail.com",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Myspace breach included SHA-1 hashes. Crack to find additional passwords.",
    },
    {
        "title": "Verify columbiadental1@yahoo.com link to Karyna Shuliak DDS program",
        "category": "connection",
        "target_name": "Karyna Shuliak",
        "description": "columbiadental1@yahoo.com suspected to be connected to Karyna Shuliak's Columbia dental program (DDS ~2015). Epstein helped her get in per Bloomberg.",
    },
    {
        "title": "Dehashed search for Karyna Shuliak / Karyna Shulyak",
        "category": "person",
        "target_name": "Karyna Shuliak",
        "description": "Epstein's last girlfriend, $100M heiress. May find additional emails, addresses, phone numbers. Try both name spellings.",
    },
    {
        "title": "Dehashed search for Tyler Shears email (LinkedIn: shearst)",
        "category": "person",
        "target_name": "Tyler Shears",
        "description": "President of Shears Consulting Group. $9,250/invoice from Epstein. Pivot from LinkedIn profile to find personal email for breach search.",
    },
    {
        "title": "Dehashed search for ssulayem@me.com and Sultan.BinSulayem@dubaiworld.ae",
        "category": "person",
        "target_name": "Sultan Bin Sulayem",
        "description": "Chairman DP World. 6,857+ jmail results with Epstein. Check for breach exposure, additional PII.",
    },
    {
        "title": "Dehashed search for Fettah Tamince — Rixos Hotels CEO",
        "category": "person",
        "target_name": "Fettah Tamince",
        "description": "Facilitated placement of Epstein's Russian masseuse at Rixos Belek. Full 'Training Program' operation documented (Jun-Jul 2017). 78 jmail results.",
    },
    {
        "title": "Download DOJ PDF EFTA02387318 — masseuse passport less redacted?",
        "category": "document",
        "target_name": "Fettah Tamince",
        "description": "Vol 11 document. Russian masseuse passport may be less redacted in original DOJ PDF vs jmail rendering.",
    },
    {
        "title": "Download DOJ PDF EFTA01043063 — 'work both of the girls very hard' email",
        "category": "document",
        "target_name": "Jeffrey Epstein",
        "description": "Vol 9 document. Email about working 'both of the girls very hard.' Important for understanding labor/exploitation patterns.",
    },
    {
        "title": "Dehashed search for Sebla Soydan and Elif Ceylan (Rixos staff)",
        "category": "person",
        "target_name": "Fettah Tamince",
        "description": "Sebla Soydan = Rixos PA. Elif Ceylan = Rixos Belek contact. May have personal emails revealing more about 'Training Program' operation.",
    },
    {
        "title": "Investigate phone 212-772-9416 — Epstein gave to Sultan (Jan 2011)",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Is this NES LLC / 9 E 71st? Or another Epstein entity? Cross-reference with known phone numbers.",
    },
    {
        "title": "Dehashed search for Dan Fleuette / doitfluet (ProtonMail)",
        "category": "person",
        "target_name": "Dan Fleuette",
        "description": "Steve Bannon's War Room producer. Traveled with Epstein May 2019. ProtonMail user. 31 DOJ Vol 11 docs.",
    },
    {
        "title": "Dehashed search for Maxim Churkin",
        "category": "person",
        "target_name": "Maxim Churkin",
        "description": "Son of Russia's UN Ambassador Vitaly Churkin. Father introduced to Epstein May 2016. Laptop gift, Dubin firm placement attempt, Hasty Pudding. Still active Jun 2019.",
    },
    {
        "title": "Dehashed search for Natalia Molotkova — AmEx Centurion travel manager",
        "category": "person",
        "target_name": "Natalia Molotkova",
        "description": "93 emails. Booked Bannon Norway trip, Odessa flights. Key logistics person who arranged travel for Epstein associates.",
    },
    {
        "title": "Search jmail for Dain Valverde — third traveler on Bannon Norway trip",
        "category": "person",
        "target_name": "Dain Valverde",
        "description": "Traveled with Bannon and Fleuette to Bergen Norway May 4-6 2019. Passport submitted to Epstein. Identity unclear.",
    },
    {
        "title": "Search jmail for Kathy Ruemmler person page",
        "category": "person",
        "target_name": "Kathy Ruemmler",
        "description": "Obama's White House Counsel, Goldman Sachs GC. Called Epstein 'Uncle Jeffrey'. 201 HF emails. Organized Bannon-Lajcak dinner. jmail page not yet reviewed.",
    },
    {
        "title": "Search jmail for Jide Zeitlin — Goldman partner, Tapestry CEO",
        "category": "person",
        "target_name": "Jide Zeitlin",
        "description": "Goldman Sachs partner. 153 Glencore documents. Sultan/Deripaska/Glencore bridge. Tapestry CEO. jmail page not yet reviewed.",
    },
    {
        "title": "Search jmail for Miroslav Lajcak — UN GA President",
        "category": "person",
        "target_name": "Miroslav Lajcak",
        "description": "UN General Assembly President. Resigned after Epstein files released. Epstein arranged dinner with Bannon via Ruemmler.",
    },
    {
        "title": "Browse Bannon jmail pages 3-6 (pages 1-2 = Apr 2019 – Jul 2018)",
        "category": "person",
        "target_name": "Steve Bannon",
        "description": "526 jmail emails total. Only pages 1-2 documented (most recent). Earlier correspondence may reveal origin of relationship.",
    },
    {
        "title": "Review Karyna Shuliak jmail pages 2-84 (8,210 more emails)",
        "category": "person",
        "target_name": "Karyna Shuliak",
        "description": "8,310 total emails, only page 1 reviewed. De facto property manager/Chief of Staff. Remaining 83 pages may reveal property details, staff, financial flows.",
    },
    {
        "title": "Query DOJ Vol 11 for Thorbjorn Jagland full correspondence",
        "category": "person",
        "target_name": "Thorbjorn Jagland",
        "description": "Secretary General of Council of Europe. 20+ emails with Epstein. Part of Rod-Larsen European network.",
    },
    {
        "title": "Query for Benjamin Harnwell — Bannon's Movement associate",
        "category": "person",
        "target_name": "Benjamin Harnwell",
        "description": "Translated Italian press about Bannon's populist operations. Connect to Bannon-Epstein nexus.",
    },
    {
        "title": "Dehashed search for anasalrasheed@gmail.com",
        "category": "person",
        "target_name": "Anas Alrasheed",
        "description": "Kuwaiti envoy. 70 emails with Epstein. Yemen peace talks, Qatar crisis intelligence, MBS-Trump scandal intel.",
    },
    {
        "title": "Dehashed search for bkarp@paulweiss.com — resigned Paul Weiss chairman",
        "category": "person",
        "target_name": "Brad Karp",
        "description": "19 Epstein emails. Resigned as Paul Weiss chairman. Groff was 'associate' at Paul Weiss.",
    },
    {
        "title": "Dehashed search for Samantha Rose Stein (ProtonMail)",
        "category": "person",
        "target_name": "Samantha Rose Stein",
        "description": "ProtonMail user. Apartment arranged by Epstein. 50 documents in DOJ Vol 11. Possible underage victim or associate.",
    },
    {
        "title": "Dehashed search for b.liliyal3@yandex.ru — Deripaska associate",
        "category": "person",
        "target_name": "Lilia (Deripaska)",
        "description": "Woman from WEF who worked for Deripaska. Presented to Epstein with CV/photos. 'she is not 24 though sorry for that.' EFTA02336502.",
    },
    {
        "title": "Investigate Kare Moljord — $18M 'craft purchase' lawyer",
        "category": "financial",
        "target_name": "Kare Moljord",
        "description": "Norwegian Supreme Court lawyer coordinating $18M 'craft purchase' between Epstein and Rod-Larsen. 2,110 Rod-Larsen docs in DOJ Vol 11.",
    },
    {
        "title": "Investigate Morits Skaugen — Norwegian shipping magnate",
        "category": "financial",
        "target_name": "Morits Skaugen",
        "description": "Involved in Rod-Larsen 'craft purchase' timing. Part of Norwegian financial network.",
    },
    {
        "title": "Investigate Aziza Alahmadi — Saudi Arabia advisory conduit",
        "category": "person",
        "target_name": "Aziza Alahmadi",
        "description": "Epstein's Saudi Arabia advisory conduit. CC'd Rod-Larsen on Aramco pitch.",
    },
    {
        "title": "Query DOJ Vol 11 for full 'Dear Bill' Gates Foundation letter (EFTA02497615)",
        "category": "document",
        "target_name": "Rod-Larsen",
        "description": "Rod-Larsen editing Epstein's letter to Bill Gates about pandemic keynote. Cross-ref with Christopher Elias / Gates Foundation Global Development.",
    },
    {
        "title": "Investigate Christopher Elias / Gates Foundation Global Development",
        "category": "person",
        "target_name": "Christopher Elias",
        "description": "Referenced in Epstein's Gates Foundation letter. IPI meetings. Connection between Epstein-Rod-Larsen-Gates Foundation.",
    },
    {
        "title": "Dehashed search for epstein@wanadoo.fr",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Newly discovered French email address from DDoSecrets EML corpus.",
    },
    {
        "title": "Dehashed search for littlestjeff@yahoo.com and manager@littlestjeff.com",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Newly discovered Epstein-controlled accounts from DDoSecrets EML corpus.",
    },
    {
        "title": "WHOIS history for littlestjeff.com",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Epstein-controlled domain. Registration details may reveal hosting, registrant info, timeline.",
    },
    {
        "title": "Query DOJ Vol 11 for Borge Brende (WEF President) correspondence",
        "category": "person",
        "target_name": "Borge Brende",
        "description": "WEF President. Dinner with Epstein, Rod-Larsen, Wolff. Cross-reference with European elite network.",
    },
    {
        "title": "Query DOJ Vol 11 for June 20 2019 Paris dinner guest list",
        "category": "document",
        "target_name": "Rod-Larsen",
        "description": "Rod-Larsen + Jagland confirmed at Paris dinner 1 month before Epstein arrest. Who else attended?",
    },
    {
        "title": "Investigate Glenn Dubin / Maxim Churkin placement",
        "category": "connection",
        "target_name": "Glenn Dubin",
        "description": "Epstein tried to get Maxim Churkin internship at Dubin's firm. Dubin already known associate. Connection between Russian ambassador's son and Dubin financial network.",
    },
]

MEDIUM_PRIORITY = [
    {
        "title": "Check Geocaching profile JeeVacation",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Login required. Sherlock found profile. May reveal location history.",
    },
    {
        "title": "Check Google Maps contributions for jeevacation1 (Google ID 100862769353613298334)",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Google ID known from Epieos. May have location reviews.",
    },
    {
        "title": "Check Pinterest profile for zorroranch",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Browser required. Sherlock found profile.",
    },
    {
        "title": "WHOIS history for jeeproject.com",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Registration details may reveal hosting, registrant info.",
    },
    {
        "title": "Narrow down je*****@btopenworld.com backup email",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Partial email from breach data. BT Openworld = UK ISP. Need to determine full address.",
    },
    {
        "title": "Investigate Neptune Industries / neptuneindustries.com further",
        "category": "entity",
        "target_name": "Neptune Industries",
        "description": "Officers Michael Joubert, Steve Carbone. Part of corporate structure discovered via (212) 971-1307 pivot.",
    },
    {
        "title": "Dehashed search for phone (732) 634-1230 (Marinese home)",
        "category": "digital",
        "target_name": "Gie Marinese",
        "description": "Marinese was Epstein bookkeeper 1993-2000. Home phone may reveal family members, associated accounts.",
    },
    {
        "title": "Dehashed search for John J. Marinese",
        "category": "person",
        "target_name": "John J. Marinese",
        "description": "Husband of Gie Marinese (bookkeeper). Corporate registrations, property records.",
    },
    {
        "title": "Property records for 304 Maple Hill Dr, Woodbridge NJ",
        "category": "financial",
        "target_name": "Gie Marinese",
        "description": "Marinese residence. Current ownership, any transfers.",
    },
    {
        "title": "Investigate Erika Kellerhals — Financial Strategy Group",
        "category": "entity",
        "target_name": "Erika Kellerhals",
        "description": "Incorporated Financial Strategy Group Ltd (became Southern Country International). Part of offshore structure.",
    },
    {
        "title": "Dehashed search for entity names (Southern Financial, Financial Trust, Prytanee, etc.)",
        "category": "entity",
        "target_name": "Jeffrey Epstein",
        "description": "Search: Southern Financial, Financial Trust Company, Prytanee LLC, IGO Company, Ellmax, Thomas World Air.",
    },
    {
        "title": "Investigate Jay Lefkowitz — Kirkland & Ellis, Epstein 2008 plea",
        "category": "person",
        "target_name": "Jay Lefkowitz",
        "description": "Kirkland & Ellis partner. Represented Epstein 2008. Connected to Zwirn fund emails.",
    },
    {
        "title": "Dehashed search for Daphne Wallace",
        "category": "person",
        "target_name": "Daphne Wallace",
        "description": "Confirmed Epstein logistics person (US side). karishulia Houzz follower. Breach databases may reveal more.",
    },
    {
        "title": "Search for marc_nyc / Marc Boutges — karishulia Houzz follower",
        "category": "person",
        "target_name": "Marc Boutges",
        "description": "Follows 47 accounts including karishulia on Houzz. Possible inner circle.",
    },
]

LOW_PRIORITY = [
    {
        "title": "Investigate Flickr Yahoo auth connection for jeffreyepsteinorg@yahoo.com",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Flickr uses Yahoo login. jeffreyepsteinorg@yahoo.com may have a Flickr account with photos.",
    },
    {
        "title": "Run Epieos on remaining emails (jeeholidays, zorroranch, columbiadental1, jeevacation@me.com)",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Web-based tool. Check for Google ID, HIBP breaches, service registrations.",
    },
    {
        "title": "Dehashed search for augsteins@ emails (password coincidence check)",
        "category": "digital",
        "target_name": "Jeffrey Epstein",
        "description": "Determine if augsteins@ variants are connected or just credential stuffing from password reuse.",
    },
    {
        "title": "Search breach databases for Sarah Kellen, Svetlana Pozhidaeva, Faith Kates, Jeff Fuller",
        "category": "person",
        "target_name": "Jeffrey Epstein",
        "description": "Richard Kahn's Houzz follows = Epstein inner circle. Check for breach exposure.",
    },
    {
        "title": "Investigate Black Bag Media (Arlington VA) — $100K Epstein wire",
        "category": "entity",
        "target_name": "Black Bag Media",
        "description": "Received $100K wire transfer from Epstein. Arlington VA location. Purpose unknown.",
    },
    {
        "title": "Look up Ranch Lake II/III Inc — Colorado/Aspen property",
        "category": "entity",
        "target_name": "Jeffrey Epstein",
        "description": "Rarely discussed in coverage. Colorado/Aspen property connection.",
    },
    {
        "title": "Search for Libet Johnson + Epstein property connections beyond Vail",
        "category": "connection",
        "target_name": "Libet Johnson",
        "description": "Known Vail property connection. May have broader real estate network ties.",
    },
    {
        "title": "Check karishulia Houzz ideabooks (55 items)",
        "category": "digital",
        "target_name": "Karyna Shuliak",
        "description": "May reveal specific properties being designed/renovated. 'karishulia's ideas' has 55 items.",
    },
]

# ── Intelligence-derived leads (from confirmed findings not yet pursued) ──

INTELLIGENCE_LEADS = [
    {
        "title": "Investigate Rod-Larsen full financial network ($18M craft purchase, Gates Foundation, Saudi Aramco)",
        "category": "financial",
        "priority": "critical",
        "target_name": "Terje Rod-Larsen",
        "description": "2,110 DOJ Vol 11 docs. $18M 'craft purchase' with Moljord/Skaugen. Gates Foundation 'Dear Bill' letter. Saudi Aramco advisory. Zuckerman cognitive intervention. June 20 2019 Paris dinner 1 month before arrest. IPI head. Most documented financial relationship in corpus.",
    },
    {
        "title": "Map complete Deripaska/Lilia WEF recruitment pipeline",
        "category": "connection",
        "priority": "critical",
        "target_name": "Oleg Deripaska",
        "description": "Woman from WEF who worked for Deripaska presented to Epstein with CV/photos via yandex.ru. 'she is not 24 though sorry for that.' EFTA02336502. 14 Deripaska docs in DOJ Vol 11. Zeitlin connection to both Deripaska and Glencore.",
    },
    {
        "title": "Investigate Ken Starr / AG strategizing with Epstein (Dec 6-7 2018)",
        "category": "legal",
        "priority": "high",
        "target_name": "Ken Starr",
        "description": "Starr and Epstein strategizing about Mueller/AG as Barr nominated. Epstein editing Ruemmler's AG-decline statement. Direct evidence of political influence operation.",
    },
    {
        "title": "Investigate ProtonMail network — Samantha Stein, Dan Fleuette, David Stern",
        "category": "digital",
        "priority": "high",
        "target_name": "Samantha Rose Stein",
        "description": "93 ProtonMail docs in DOJ Vol 11. Samantha Rose Stein (50 docs, apartment arranged). Dan Fleuette/doitfluet (31 docs). David Stern recommended 'the safest email.' Why did Epstein switch to encrypted comms?",
    },
    {
        "title": "Trace Goldman Sachs triple node (Ruemmler, Zeitlin, Daffey)",
        "category": "financial",
        "priority": "high",
        "target_name": "Goldman Sachs",
        "description": "Three Goldman connections: Ruemmler (GC, 'Uncle Jeffrey', Bannon-Lajcak dinner), Zeitlin (partner, Sultan/Deripaska/Glencore bridge), Daffey (exec, bought 9 E 71st for $51M). Systematic Goldman relationship.",
    },
    {
        "title": "Map Weingarten / Trump counsel decision chain (Feb-May 2017)",
        "category": "legal",
        "priority": "high",
        "target_name": "Reid Weingarten",
        "description": "245 emails. Trump/Kushner counsel finalist (May 2017). Flynn decision (Feb 2017). Mueller strategy. Bannon breakfast planned July 7 (day after arrest). Barrack/Chagoury referral pipeline.",
    },
    {
        "title": "Investigate Wolff/Epstein Rybolovlev manuscript — Epstein as eyewitness",
        "category": "document",
        "priority": "high",
        "target_name": "Michael Wolff",
        "description": "303 emails. Wolff sent Rybolovlev/Trump $96M property flip draft chapter to Epstein (Feb 2019). Bannon: 'You were the one person I was truly afraid of coming forward during the campaign.' Epstein was eyewitness to the deal.",
    },
    {
        "title": "Investigate Landon Thomas SoftBank/Gates quid pro quo",
        "category": "connection",
        "priority": "high",
        "target_name": "Landon Thomas Jr.",
        "description": "185 emails. NYT reporter brokered Masa/Son introduction (Mar 2017). 'does my story on Abraaj get me a meeting with Gates?' — explicit quid pro quo. Told Masa's people about 'Saudis/Gates/Trump crowd.'",
    },
    {
        "title": "Investigate Elisa New / Harvard / Templeton Foundation network",
        "category": "person",
        "priority": "medium",
        "target_name": "Elisa New",
        "description": "72 emails. Harvard professor, Summers' wife. $100K+ donor, Templeton intermediary. Woody Allen footage, Serena Williams recruitment. Celebrity roster: Clinton, Gore, Kagan, Kissinger, Biden, Bono.",
    },
    {
        "title": "Investigate David Schoen dual role (Epstein counsel + Hannity anti-Mueller + Trump impeachment)",
        "category": "legal",
        "priority": "medium",
        "target_name": "David Schoen",
        "description": "34 emails. Auditioned to represent Epstein while appearing on Hannity attacking Mueller. Later became Trump's 2nd impeachment attorney. Track the timeline of dual roles.",
    },
    {
        "title": "ICIJ cross-reference all Epstein entities against offshore leaks",
        "category": "entity",
        "priority": "high",
        "target_name": "Jeffrey Epstein",
        "description": "Liquid Funding already found in Paradise Papers. Need systematic search of all known Epstein entities against 800K ICIJ records. Requires Neo4j running.",
    },
    {
        "title": "Ingest EpsteinExposed.com data (1,404 persons, 143K docs, 1,708 flights)",
        "category": "document",
        "priority": "high",
        "target_name": "Jeffrey Epstein",
        "description": "Potentially the most useful external source — pre-built entity resolution connecting flight logs to court docs to emails to black book. GitHub: epsteinexposed/epstein-exposed.",
    },
    {
        "title": "Ingest Giuffre v. Maxwell docket (SDNY 15-cv-7433) — civil depositions",
        "category": "document",
        "priority": "high",
        "target_name": "Jeffrey Epstein",
        "description": "Jan 2024 unsealing + Jul 2025 Second Circuit. Depositions (Maxwell, Giuffre, staff). Different provenance from DOJ/EFTA — civil litigation vs criminal investigation.",
    },
    {
        "title": "Ingest USVI v. JPMorgan exhibits (SDNY 1:22-cv-10904)",
        "category": "financial",
        "priority": "high",
        "target_name": "JPMorgan Chase",
        "description": "300+ exhibits including financial records, wire transfers, Epstein-JPMC correspondence. Best source for financial evidence not in DOJ dump.",
    },
    {
        "title": "Ingest tensonaut/EPSTEIN_FILES_20K (25K OCR'd texts)",
        "category": "document",
        "priority": "medium",
        "target_name": "Jeffrey Epstein",
        "description": "Clean RAG-ready text from House Oversight release. Single CSV. May complement existing datasets.",
    },
]


def main():
    # Initialize database
    db = get_db()
    db.close()

    count = 0

    # High priority outstanding actions
    for item in HIGH_PRIORITY:
        lead_id = add_lead(
            title=item["title"],
            description=item.get("description"),
            category=item.get("category"),
            priority="high",
            source="migration:outstanding_actions",
            target_name=item.get("target_name"),
        )
        count += 1
        print(f"  [high] #{lead_id}: {item['title'][:70]}")

    # Medium priority
    for item in MEDIUM_PRIORITY:
        lead_id = add_lead(
            title=item["title"],
            description=item.get("description"),
            category=item.get("category"),
            priority="medium",
            source="migration:outstanding_actions",
            target_name=item.get("target_name"),
        )
        count += 1
        print(f"  [med]  #{lead_id}: {item['title'][:70]}")

    # Low priority
    for item in LOW_PRIORITY:
        lead_id = add_lead(
            title=item["title"],
            description=item.get("description"),
            category=item.get("category"),
            priority="low",
            source="migration:outstanding_actions",
            target_name=item.get("target_name"),
        )
        count += 1
        print(f"  [low]  #{lead_id}: {item['title'][:70]}")

    # Intelligence-derived leads
    for item in INTELLIGENCE_LEADS:
        lead_id = add_lead(
            title=item["title"],
            description=item.get("description"),
            category=item.get("category"),
            priority=item.get("priority", "high"),
            source="migration:intelligence_findings",
            target_name=item.get("target_name"),
        )
        count += 1
        prio = item.get("priority", "high")
        print(f"  [{prio[:4]}] #{lead_id}: {item['title'][:70]}")

    print(f"\nMigrated {count} leads to investigation.db")


if __name__ == "__main__":
    main()
