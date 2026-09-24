# Chief of Staff: a document-filing system built to say "I'm not sure"

Python · Claude API · Gmail API (read-only) · 126 tests

I spent about a month building a document-filing tool for a small residential real-estate brokerage. It works, it runs on real mail, and they didn't buy it. This is what I built, why I built it the way I did, and what I'd do differently.

## The problem

On a discovery call in August, three specific issues surfaced. Their admin files every contract and addendum into Google Drive by hand: download, read, rename, find the right folder. Client threads go quiet and nobody notices until the client follows up. And their client conversation is spread across email, iMessage and WhatsApp. They'd been stitching pieces of this together with Zapier.

They were clear about what they didn't need. Their leads come from word of mouth and open houses, and they already answer new inquiries within minutes. Nobody needed to sell them speed. So I scoped the build to the first problem, filing, because that's where a silent mistake costs the most.

## Failure branches before features

A tool like this is dangerous because it’s wrong quietly. A contract filed into a plausible-looking but incorrect folder gets discovered weeks later by someone looking for it. So I wrote the failure branches first and the features second.

Every fact is sourced or flagged. The model extracts four fields from a document: type, property address, client name, date. The address and client name have to appear word for word in the document’s own text. If naming the thing requires completing a partial address, resolving a pronoun, or joining two fragments, the field is null and the reason goes in a separate list. However, document type is the exception: a classification isn't a quotation, so it can't be grounded the same way. Today it's only checked against a list of four known types (contract, addendum, disclosure, amendment), which means a wrong type that happens to be on the list still passes. That's an open finding. The fix, requiring policy-defined marker phrases in the text before a type is accepted, is specified but not built yet.

Filing fails closed. Four gates: parsed, grounded, required fields present, exactly one matching folder. Folder matching is exact on a normalized address, so there’s no match-guessing or confidence scores. A document either passes all four gates and is routed to FILE, with an audit row behind it, or it’s routed to Needs Review with a reason attached. There is no third outcome. A Zapier rule structurally can't offer that, because a rule that matches a string has no way to be unsure.

The credential is read-only. The service holds ‘gmail.readonly’ and nothing else, asserted at OAuth time against the scopes the token actually came back with, before a client object exists.

Then I ran an adversarial audit against my own codebase, treating those rules as laws and hunting for places they were asserted rather than enforced. It produced 40 findings. The worst one: the plan was to request read plus compose, so the tool could leave drafts, and I'd written in two places (the Gmail adapter and the README) that the compose scope "cannot put mail on the wire." Google documents that scope as authorizing sends. No credential had been issued yet, but the load-bearing safety claim was prose asserting a property the design didn't have. The fix: compose is permanently banned, the allowed scopes are pinned to read-only in code, and an exact-match check rejects any scope nobody planned for.

27 of the 40 findings are fixed. Every behavior fix is test-first: a failing test reproduces the finding before the fix exists. The suite went from 6 tests to 126. The other 13 are logged, each with a reason it's still open.

## What got built

First, a four-screen demo showing the real decisions, including one where the system declines: a signed addendum whose address has no folder, held for review next to a similar folder for the same buyer; the refusal was the point.

Then the live engine ran real OAuth against a real Gmail account, real attachment fetch, real extraction, real gates, real audit rows. I ran it twice. The second run routed a test addendum to FILE, with all four gates passed and the destination folder and filename computed. Drive writes are the next step; for now the engine prints what it would file. The first run picked up the newest PDF in my inbox, which happened to be my own job-offer letter: 4,155 characters of perfectly legible English. All four fields came back null, each with the model's account of why, and the type check flagged it ‘unknown_doc_type.’ It's a real document, just not a real-estate one, and the system didn't invent a property address for a document that doesn't have one. I didn't set that up, and it's the best evidence I have.

## The pitch

Before the meeting I wrote a gap check, cross-referencing every item from the discovery call against what the demo actually showed, plus a reverse pass listing everything the demo claimed that they'd never asked for. The reverse pass was the more useful half. It caught a market-analysis screen nobody requested, a 7:30am email brief nobody requested, and a Drive-scoping sentence in my script that no code backed up, so I changed the sentence.

Pricing was $2,500 for a four-week pilot with two or three volunteer agents, credited in full against year one, then $450 a month flat for the office. I made it flat rather than per-seat or per-document so nothing about the price discourages adding a mailbox, and nothing rewards me for filing more documents than they have.

## The ending

They said no.

Looking back, the pitch asked them to buy four weeks of evidence. But I already had an engine that reads a real inbox and refuses real documents. The right ask was: give me twenty of last month's PDFs, and I'll show you where each one lands and why. Lead with a trial on their documents, not a paid pilot on their calendar.

The code is unchanged by that. The gates still hold. I'd just open with them.

---

I'm Josh Tseng, an applied AI engineer and Claude Certified Architect. I build AI systems for businesses that can't afford quiet mistakes. I'm looking for forward-deployed and applied AI engineering roles: www.linkedin.com/in/joshtseng-aiops · miyamotoai.com

All names, addresses, and companies in the demo are fictional.
