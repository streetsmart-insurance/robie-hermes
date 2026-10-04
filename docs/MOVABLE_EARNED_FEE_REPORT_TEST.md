# Movable earned fee report, Test only

Pure review calculation, stacked on #756. No network, authenticated source read, transfer, posting, schedule or live runner. User Oct3decision: ACH7calendar days;card0hold. Seven days is a chosen review threshold, not proven return finality. Known later card disputes remain liabilities despite no card hold.

`report(crosswalk,payments,returns,evidence,as_of=...,environment='TEST')` outputs one `movable_earned_fee` decimal string or null(`withheld_unknown`) plus exceptions, source/PSP audit and diagnostic buckets. Null is not zero. Diagnostics are never a transfer figure. Always transfer_allowed=false and all action counts zero.

#756 findings are candidates, NOT cleared deposits. Each payment requires separate full-email-ID/bank-TRN/Trust3021/stable-bank-ID clearance evidence, verified earned agency fees (NOT processor/convenience fees), previous sweeps, payment method/date, complete payout payment-line net proof, and complete current earnings/returns/clearance/trust-capacity evidence. Capacity means earned fee cash remaining after carrier obligations, prior sweeps and bank debits; it is NOT total bank balance. Assertions are an adapter contract, not source authentication. No current production amount is claimed.

ACH age is as_of calendar date minus original payment date, never payout date. Day6held,day7eligible for review if all evidence passes. Conflicting original dates are preserved; later date controls age conservatively. Card day0has no time hold. Wallets must have a verified underlying rail;unknown rail blocks. Date-only boundary needs agreed ledger timezone before live deployment;as_of and all adapter dates must use the same ledger timezone. Future/invalid dates block.

Every return/dispute/refund has stable ID, originalPSP,source,eventdate,method,state,gross liability,alreadydebited amount withdebitproof,andremaining liability. Gross=alreadydebited+remaining musttie. Return-associated fees are excluded. Remaining liabilities are netted once from mature fees; proven alreadydebited liabilities aren't deducted twice. OpenACHreturns block the whole figure. Opencarddisputes are flagged/netted but don't add a timehold. Unallocated pendingreturns,unknown liability,missingPSP,duplicates,mismatched methods or missing crosswalkchain block. Negative net is reported as shortfall and figure floors0;carrier-obligation capacity caps it.

Private55-pair replay intentionally returns null: actual crosswalk candidates exist, but independent earning/clearance/return completeness bindings do not. Keeps10PSPreturnchainrows. Synthetic fixtures prove arithmetic,policy boundaries and adverse cases;they are not real earnedfee measurements. Inputs with real financial data stay outsideGitHub.

BUILT: function,schema/guards,audit output,focused tests andisolatedTestproof.
DESIGNED/UNVERIFIED: live adapters,sourceauth/freshness/completeness,independentclearingsemantics/stableIDs,PSP-to-EZLynxearnedagencyfeebinding,carrierobligation/priorsweepcapacity,returnresolution,debitdeduplication,ledger timezone,monitoringanddurablereviewowner. Noautomatictransfer now or later without separateauthorization. ExistingnativeEZLynxreceiptsmustnotbeduplicated.

No additional Carlo decision needed for this Testdraft. Before an actionable live report: agree ledger timezone if sources can't establish it; resolve any accountingclassification/earnedfee exceptions that source research cannot settle. Those are not permission to invent amounts or deploy.
