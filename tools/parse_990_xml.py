#!/usr/bin/env python3
"""
Shared IRS 990 XML parsing — grants, officers, financials, and insider data.

Extracts from both Form 990 and Form 990-PF:
  - Schedule I (grants to organizations)
  - Schedule R (related organizations)
  - Part VII / Officer group (officers, directors, key employees + compensation)
  - Part I (financial summary: revenue, expenses, assets)
  - Part IX (functional expenses: program vs admin vs fundraising)  [990 only]
  - Schedule J (detailed executive compensation)                    [990 only]
  - Schedule L (transactions with interested persons)               [when present]
  - Part IV checklist flags (red-flag indicators)                   [990 only]

Two entry points:
    parse_filing(xml_path)       — parse from file path (used by targeted tool)
    parse_filing_bytes(data)     — parse from raw bytes (used by bulk pipeline)
"""

import xml.etree.ElementTree as ET

NS = {"irs": "http://www.irs.gov/efile"}


def _text(el, tag):
    """Get text from a child element, or empty string."""
    if el is None:
        return ""
    child = el.find(f"irs:{tag}", NS)
    if child is None:
        child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _int(el, tag):
    """Get integer from a child element, or 0."""
    val = _text(el, tag)
    if not val:
        return 0
    try:
        return int(val.replace(",", ""))
    except ValueError:
        return 0


def _float(el, tag):
    """Get float from a child element, or 0.0."""
    val = _text(el, tag)
    if not val:
        return 0.0
    try:
        return float(val.replace(",", ""))
    except ValueError:
        return 0.0


def _flag(el, tag):
    """Get boolean flag (X, 1, true → 1; else 0)."""
    val = _text(el, tag).upper()
    return 1 if val in ("X", "1", "TRUE", "YES") else 0


def _build_address(el):
    """Build address string from US or foreign address group."""
    if el is None:
        return ""
    for addr_tag in ["USAddress", "AddressUS"]:
        addr = el.find(f"irs:{addr_tag}", NS)
        if addr is not None:
            line1 = _text(addr, "AddressLine1Txt") or _text(addr, "AddressLine1")
            line2 = _text(addr, "AddressLine2Txt") or _text(addr, "AddressLine2")
            city = _text(addr, "CityNm") or _text(addr, "City")
            state = _text(addr, "StateAbbreviationCd") or _text(addr, "State")
            zip_code = _text(addr, "ZIPCd") or _text(addr, "ZIPCode")
            parts = [p for p in [line1, line2, city, state, zip_code] if p]
            return ", ".join(parts)
    for addr_tag in ["ForeignAddress", "AddressForeign"]:
        addr = el.find(f"irs:{addr_tag}", NS)
        if addr is not None:
            line1 = _text(addr, "AddressLine1Txt") or _text(addr, "AddressLine1")
            line2 = _text(addr, "AddressLine2Txt") or _text(addr, "AddressLine2")
            city = _text(addr, "CityNm") or _text(addr, "City")
            country = _text(addr, "CountryCd") or _text(addr, "Country")
            parts = [p for p in [line1, line2, city, country] if p]
            return ", ".join(parts)
    return ""


def _get_business_name(el):
    """Extract business name from BusinessName or RecipientBusinessName group."""
    if el is None:
        return ""
    for name_tag in ["RecipientBusinessName", "BusinessName", "BusinessNameLine1Txt",
                      "OrganizationBusinessName"]:
        name_el = el.find(f"irs:{name_tag}", NS)
        if name_el is not None:
            line1 = _text(name_el, "BusinessNameLine1Txt") or _text(name_el, "BusinessNameLine1")
            if line1:
                return line1
            if name_el.text:
                return name_el.text.strip()
    person = _text(el, "RecipientPersonNm")
    if person:
        return person
    return ""


def _parse_officers_990(data_el):
    """Extract Part VII Section A — officers, directors, key employees (Form 990)."""
    officers = []
    for grp in data_el.findall(".//irs:Form990PartVIISectionAGrp", NS):
        name = _text(grp, "PersonNm")
        if not name:
            name = _get_business_name(grp)
        if not name:
            continue
        comp_org = _int(grp, "ReportableCompFromOrgAmt")
        comp_related = _int(grp, "ReportableCompFromRltdOrgAmt")
        other = _int(grp, "OtherCompensationAmt")
        officers.append({
            "person_name": name,
            "title": _text(grp, "TitleTxt"),
            "avg_hours_per_week": _float(grp, "AverageHoursPerWeekRt"),
            "comp_from_org": comp_org,
            "comp_from_related": comp_related,
            "other_comp": other,
            "total_comp": comp_org + comp_related + other,
            "is_director": _flag(grp, "IndividualTrusteeOrDirectorInd"),
            "is_officer": _flag(grp, "OfficerInd"),
            "is_key_employee": _flag(grp, "KeyEmployeeInd"),
            "is_highest_comp": _flag(grp, "HighestCompensatedEmployeeInd"),
            "is_former": _flag(grp, "FormerOfcrDirectorTrusteeInd"),
        })
    return officers


def _parse_officers_990pf(data_el):
    """Extract officers/directors from Form 990-PF."""
    officers = []
    info = data_el.find(".//irs:OfficerDirTrstKeyEmplInfoGrp", NS)
    if info is None:
        return officers
    for grp in info.findall("irs:OfficerDirTrstKeyEmplGrp", NS):
        name = _text(grp, "PersonNm")
        if not name:
            name = _get_business_name(grp)
        if not name:
            continue
        comp = _int(grp, "CompensationAmt")
        benefits = _int(grp, "EmployeeBenefitProgramAmt")
        expense_acct = _int(grp, "ExpenseAccountOtherAllwncAmt")
        title = _text(grp, "TitleTxt")
        officers.append({
            "person_name": name,
            "title": title,
            "avg_hours_per_week": _float(grp, "AverageHrsPerWkDevotedToPosRt"),
            "comp_from_org": comp,
            "comp_from_related": 0,
            "other_comp": benefits + expense_acct,
            "total_comp": comp + benefits + expense_acct,
            "is_director": 1 if "DIRECTOR" in title.upper() or "TRUSTEE" in title.upper() else 0,
            "is_officer": 1 if any(t in title.upper() for t in
                                   ["PRESIDENT", "VP", "VICE", "SECRETARY", "TREASURER",
                                    "CEO", "CFO", "COO", "CAO"]) else 0,
            "is_key_employee": 0,
            "is_highest_comp": 0,
            "is_former": 1 if "FORMER" in title.upper() or "TERM END" in name.upper() or "END " in name.upper() else 0,
        })
    return officers


def _parse_financials_990(data_el):
    """Extract Part I (summary) + Part IX (functional expenses) from Form 990."""
    total_exp = _int(data_el, "CYTotalExpensesAmt")
    program = _int(data_el, "TotalProgramServiceExpensesAmt")

    # Part IX functional expenses group
    func_grp = data_el.find(".//irs:TotalFunctionalExpensesGrp", NS)
    func_total = _int(func_grp, "TotalAmt") if func_grp is not None else 0
    func_program = _int(func_grp, "ProgramServicesAmt") if func_grp is not None else 0
    func_mgmt = _int(func_grp, "ManagementAndGeneralAmt") if func_grp is not None else 0
    func_fund = _int(func_grp, "FundraisingAmt") if func_grp is not None else 0

    # Use Part IX values if available, fall back to Part I
    prog_exp = func_program or program
    total_func = func_total or total_exp

    financials = {
        "total_revenue": _int(data_el, "CYTotalRevenueAmt"),
        "total_expenses": total_exp,
        "revenue_less_expenses": _int(data_el, "CYRevenuesLessExpensesAmt"),
        "contributions_grants": _int(data_el, "CYContributionsGrantsAmt"),
        "program_service_revenue": _int(data_el, "CYProgramServiceRevenueAmt"),
        "investment_income": _int(data_el, "CYInvestmentIncomeAmt"),
        "total_functional_expenses": total_func,
        "program_expenses": prog_exp,
        "management_expenses": func_mgmt,
        "fundraising_expenses": func_fund or _int(data_el, "CYTotalFundraisingExpenseAmt"),
        "total_assets_eoy": _int(data_el, "TotalAssetsEOYAmt"),
        "total_liabilities_eoy": _int(data_el, "TotalLiabilitiesEOYAmt"),
        "net_assets_eoy": _int(data_el, "NetAssetsOrFundBalancesEOYAmt"),
        "qualifying_distributions": 0,
        "net_investment_income": 0,
    }

    # Compute ratios
    denom = total_func or total_exp
    if denom and denom > 0:
        financials["program_expense_ratio"] = round(prog_exp / denom, 4) if prog_exp else 0.0
        financials["fundraising_ratio"] = round(financials["fundraising_expenses"] / denom, 4) if financials["fundraising_expenses"] else 0.0
        financials["admin_expense_ratio"] = round(func_mgmt / denom, 4) if func_mgmt else 0.0
    else:
        financials["program_expense_ratio"] = None
        financials["fundraising_ratio"] = None
        financials["admin_expense_ratio"] = None

    return financials


def _parse_financials_990pf(data_el):
    """Extract financial summary from Form 990-PF.

    990-PF elements are nested inside groups (AnalysisOfRevenueAndExpenses,
    BalanceSheetGroup, etc.), so we search recursively with .//
    """
    def _deep_int(tag):
        """Search recursively for an element and return its int value."""
        el = data_el.find(f".//irs:{tag}", NS)
        if el is None:
            el = data_el.find(f".//{tag}")
        if el is not None and el.text:
            try:
                return int(el.text.strip().replace(",", ""))
            except ValueError:
                return 0
        return 0

    total_revenue = _deep_int("TotalRevAndExpnssAmt")
    total_exp = _deep_int("TotalExpensesRevAndExpnssAmt")
    total_assets = _deep_int("TotalAssetsEOYAmt")
    total_liab = _deep_int("TotalLiabilitiesEOYAmt")

    financials = {
        "total_revenue": total_revenue,
        "total_expenses": total_exp,
        "revenue_less_expenses": _deep_int("ExcessRevenueOverExpensesAmt"),
        "contributions_grants": _deep_int("ContriRcvdRevAndExpnssAmt") or _deep_int("ContributionsRcvdAmt"),
        "program_service_revenue": 0,
        "investment_income": _deep_int("NetInvestmentIncomeAmt"),
        "total_functional_expenses": 0,
        "program_expenses": _deep_int("TotalExpensesDsbrsChrtblAmt"),
        "management_expenses": 0,
        "fundraising_expenses": 0,
        "total_assets_eoy": total_assets,
        "total_liabilities_eoy": total_liab,
        "net_assets_eoy": _deep_int("TotNetAstOrFundBalancesEOYAmt") or (total_assets - total_liab),
        "qualifying_distributions": _deep_int("QualifyingDistributionsAmt"),
        "net_investment_income": _deep_int("NetInvestmentIncomeAmt"),
        "program_expense_ratio": None,
        "fundraising_ratio": None,
        "admin_expense_ratio": None,
    }
    return financials


def _parse_schedule_j(root):
    """Extract Schedule J — detailed compensation for officers/key employees."""
    sched_j = root.find(".//irs:IRS990ScheduleJ", NS)
    if sched_j is None:
        return []
    entries = []
    for grp in sched_j.findall(".//irs:RltdOrgOfficerTrstKeyEmplGrp", NS):
        name = _text(grp, "PersonNm")
        if not name:
            continue
        entries.append({
            "person_name": name,
            "title": _text(grp, "TitleTxt"),
            "base_comp": _int(grp, "BaseCompensationFilingOrgAmt"),
            "bonus": _int(grp, "BonusFilingOrganizationAmount"),
            "other_comp": _int(grp, "OtherCompensationFilingOrgAmt"),
            "deferred_comp": _int(grp, "DeferredCompensationFlngOrgAmt"),
            "nontaxable_benefits": _int(grp, "NontaxableBenefitsFilingOrgAmt"),
            "total_comp_from_org": _int(grp, "TotalCompensationFilingOrgAmt"),
            "total_comp_from_related": _int(grp, "TotalCompensationRltdOrgsAmt"),
        })
    return entries


def _parse_schedule_l(root):
    """Extract Schedule L — transactions with interested persons."""
    sched_l = root.find(".//irs:IRS990ScheduleL", NS)
    if sched_l is None:
        return []
    transactions = []

    # Part I: Excess benefit transactions
    for grp in (sched_l.findall(".//irs:ExcessBenefitTransactionsGrp", NS) or []):
        transactions.append({
            "transaction_type": "excess_benefit",
            "person_name": _text(grp, "PersonNm") or _get_business_name(grp),
            "relationship": _text(grp, "RlnDescriptionTxt"),
            "amount": _int(grp, "TransactionAmt"),
            "description": _text(grp, "DescriptionOfTransactionTxt"),
        })

    # Part II: Loans to/from interested persons
    for grp in (sched_l.findall(".//irs:LoansBtwnOrgInterestedPrsnGrp", NS) or []):
        transactions.append({
            "transaction_type": "loan",
            "person_name": _text(grp, "PersonNm") or _get_business_name(grp),
            "relationship": _text(grp, "RelationshipWithOrgTxt"),
            "amount": _int(grp, "OriginalPrincipalAmt") or _int(grp, "LoanBalanceDueAmt"),
            "description": _text(grp, "LoanPurposeTxt"),
        })

    # Part III: Grants/assistance to interested persons
    for grp in (sched_l.findall(".//irs:GrntAsstBnftInterestedPrsnGrp", NS) or []):
        transactions.append({
            "transaction_type": "grant_to_insider",
            "person_name": _text(grp, "PersonNm") or _get_business_name(grp),
            "relationship": _text(grp, "RelationshipWithOrgTxt"),
            "amount": _int(grp, "CashGrantAmt") or _int(grp, "AssistanceAmountTxt"),
            "description": _text(grp, "PurposeOfAssistanceTxt"),
        })

    # Part IV: Business transactions with interested persons
    for grp in (sched_l.findall(".//irs:BusTrnsfrInvolvingInterestedPrsnGrp", NS) or []):
        transactions.append({
            "transaction_type": "business_transaction",
            "person_name": _text(grp, "NameOfInterestedPersonTxt") or _text(grp, "PersonNm") or _get_business_name(grp),
            "relationship": _text(grp, "RelationshipDescriptionTxt"),
            "amount": _int(grp, "AmountOfTransactionAmt"),
            "description": _text(grp, "DescriptionOfTransactionTxt"),
        })

    return transactions


def _parse_checklist(data_el):
    """Extract Part IV checklist red-flag indicators (Form 990)."""
    return {
        "excess_benefit_transaction": _flag(data_el, "EngagedInExcessBenefitTransInd"),
        "schedule_j_required": _flag(data_el, "ScheduleJRequiredInd"),
        "whistleblower_policy": _flag(data_el, "WhistleblowerPolicyInd"),
        "document_retention_policy": _flag(data_el, "DocumentRetentionPolicyInd"),
        "compensation_process_ceo": _flag(data_el, "CompensationProcessCEOInd"),
        "conflict_of_interest_policy": _flag(data_el, "ConflictOfInterestPolicyInd"),
    }


def _parse_root(root):
    """Parse grants, officers, financials, and related data from a 990 XML root."""
    header = root.find(".//irs:ReturnHeader", NS)
    filer = root.find(".//irs:Filer", NS) if header is not None else None

    ein = _text(filer, "EIN") if filer is not None else ""
    filer_name = ""
    if filer is not None:
        name_el = filer.find(".//irs:BusinessName", NS)
        if name_el is not None:
            filer_name = _text(name_el, "BusinessNameLine1Txt") or _text(name_el, "BusinessNameLine1")
    tax_period = (_text(header, "TaxPeriodEndDt") or _text(header, "TaxPeriodEndDate")) if header is not None else ""
    return_type = (_text(header, "ReturnTypeCd") or _text(header, "ReturnType")) if header is not None else ""

    result = {
        "ein": ein,
        "filer_name": filer_name,
        "tax_period": tax_period,
        "return_type": return_type,
        "grants": [],
        "related_orgs": [],
        "officers": [],
        "financials": {},
        "schedule_j": [],
        "schedule_l": [],
        "checklist_flags": {},
    }

    # ── Part VII / Officers ──
    data_990 = root.find(".//irs:IRS990", NS)
    data_990pf = root.find(".//irs:IRS990PF", NS)

    if data_990 is not None:
        result["officers"] = _parse_officers_990(data_990)
        result["financials"] = _parse_financials_990(data_990)
        result["checklist_flags"] = _parse_checklist(data_990)
    elif data_990pf is not None:
        result["officers"] = _parse_officers_990pf(data_990pf)
        result["financials"] = _parse_financials_990pf(data_990pf)

    # ── Schedule J (detailed compensation) ──
    result["schedule_j"] = _parse_schedule_j(root)

    # ── Schedule L (insider transactions) ──
    result["schedule_l"] = _parse_schedule_l(root)

    # Schedule I (990): Grants to Organizations
    sched_i = root.find(".//irs:IRS990ScheduleI", NS)
    if sched_i is not None:
        for grant in sched_i.findall(".//irs:RecipientTable", NS):
            recipient_name = _get_business_name(grant)
            if not recipient_name:
                recipient_name = _text(grant, "RecipientPersonNm")
            result["grants"].append({
                "recipient_name": recipient_name,
                "recipient_ein": _text(grant, "RecipientEIN"),
                "recipient_address": _build_address(grant),
                "cash_amount": _int(grant, "CashGrantAmt"),
                "non_cash_amount": _int(grant, "NonCashAssistanceAmt"),
                "purpose": _text(grant, "PurposeOfGrantTxt"),
                "recipient_type": "organization" if _text(grant, "RecipientEIN") else "individual",
            })

    # Schedule I variant (990-PF): Grants
    pf = root.find(".//irs:IRS990PF", NS)
    if pf is not None:
        for grant in pf.findall(".//irs:GrantOrContributionPdDurYrGrp", NS):
            recipient_name = _get_business_name(grant)
            result["grants"].append({
                "recipient_name": recipient_name,
                "recipient_ein": _text(grant, "RecipientEIN"),
                "recipient_address": _build_address(grant),
                "cash_amount": _int(grant, "Amt"),
                "non_cash_amount": 0,
                "purpose": _text(grant, "GrantOrContributionPurposeTxt"),
                "recipient_type": "organization",
            })

    # Schedule R: Related Organizations
    sched_r = root.find(".//irs:IRS990ScheduleR", NS)
    if sched_r is not None:
        rel_groups = [
            ("IdDisregardedEntitiesGrp", "disregarded_entity"),
            ("RelatedTaxExemptOrgGrp", "related_tax_exempt"),
            ("RelatedOrgCtrlEntityGrp", "related_taxable"),
            ("TrnsfrTransRlnspNonExmptGrp", "transaction_partner"),
        ]
        for group_tag, rel_type in rel_groups:
            for org in sched_r.findall(f".//irs:{group_tag}", NS):
                related_name = _get_business_name(org)
                result["related_orgs"].append({
                    "related_name": related_name,
                    "related_ein": _text(org, "EIN") or _text(org, "EINOfRelatedOrgTxt"),
                    "related_address": _build_address(org),
                    "relationship_type": rel_type,
                    "primary_activities": _text(org, "PrimaryActivitiesTxt"),
                    "legal_domicile": _text(org, "LegalDomicileStateCd") or _text(org, "LegalDomicileCountryCd"),
                    "total_income": _int(org, "TotalIncomeAmt"),
                    "end_of_year_assets": _int(org, "EndOfYearAssetsAmt"),
                    "direct_controlling_entity": (
                        _text(org, "DirectControllingEntityName") or
                        _get_business_name(org.find("irs:DirectControllingEntityName", NS))
                        if org.find("irs:DirectControllingEntityName", NS) is not None
                        else _text(org, "DirectControllingNm")
                    ),
                })

    return result


def parse_filing(xml_path):
    """Parse a 990 XML filing from a file path."""
    tree = ET.parse(xml_path)
    return _parse_root(tree.getroot())


def parse_filing_bytes(data):
    """Parse a 990 XML filing from raw bytes.

    Handles UTF-8 BOM prefix that some IRS XMLs include.
    """
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    root = ET.fromstring(data)
    return _parse_root(root)


def has_grant_data(xml_bytes):
    """Fast byte-scan to check if XML likely contains grant or related-org data.

    Avoids full XML parsing for ~70% of filings that lack Schedule I/R.
    """
    return (b"<IRS990ScheduleI" in xml_bytes or
            b"GrantOrContribution" in xml_bytes or
            b"<IRS990ScheduleR" in xml_bytes)
