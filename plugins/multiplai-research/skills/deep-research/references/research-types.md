# Research Type Configurations

## company

**Use when:** Researching a company for job applications, investment decisions, or competitive analysis.

**Staleness threshold:** 90 days for facts (funding, headcount), 7 days for news

**Domain preferences:**
- Company's official website (about, careers, blog, investor relations)
- Crunchbase (funding, investors, growth)
- LinkedIn (employee count, hiring trends, leadership)
- Glassdoor (culture, interview process, salaries)
- Recent news articles
- TechCrunch, industry publications

**Reputation notes:**
- Company's own site = authoritative for official facts
- Crunchbase/LinkedIn = established for company data
- Glassdoor = established but treat extremes with skepticism
- News = established if major outlet, emerging if niche publication

**Sub-question patterns:**
1. What does [company] do and what are their main products/services?
2. What is [company]'s funding history and financial position?
3. What is the company culture and employee sentiment at [company]?
4. Who are the key leaders at [company] and what's their background?
5. What recent news or developments involve [company]?

**Discovery questions to consider:**
- What role level are you considering? (affects culture/comp focus)
- Any specific concerns? (e.g., stability, growth, WLB)
- Competitor context needed?

**Quality signals:**
- Recency: company info can become stale quickly
- Multiple sources agreeing on key facts
- Official sources for factual claims (funding, headcount)
- Employee reviews for culture (with skepticism for extremes)

**Diversity requirements:**
- Must include company's official source if available
- At least 2 external perspectives (news, reviews, databases)
- For culture research, seek both positive and negative reviews

**Contrarian search patterns:**
- "why [company] fails"
- "[company] layoffs" / "[company] downsizing"
- "[company] culture problems"
- "leaving [company] for"
- "[company] competitors better than"
- "[company] criticism" / "[company] controversy"
- "regret joining [company]"

**Directories & aggregators:**
- Crunchbase (funding, investors, competitors, similar companies)
- LinkedIn (employee count, hiring patterns, leadership changes)
- Glassdoor (reviews, salary data, interview questions)
- Product Hunt (product launches, user reception)
- G2/Capterra (enterprise software reviews and comparisons)
- Patent databases (USPTO, Google Patents) for IP-heavy companies

**Graph traversal seeds:**
- Investors → other portfolio companies in same space
- Founders → previous companies, co-founders' other ventures
- "Competitors of [company]", "alternatives to [product]"

---

## job-market

**Use when:** Exploring career opportunities, comparing locations, or understanding industry trends.

**Staleness threshold:** 30 days (job market changes rapidly)

**Domain preferences:**
- LinkedIn job postings and salary insights
- Glassdoor salary data and job listings
- Indeed, local job boards
- Industry salary surveys (Levels.fyi for tech)
- Government labor statistics
- Cost of living comparisons (Numbeo, local sources)
- Expat forums and relocation guides

**Reputation notes:**
- Government labor stats = authoritative
- Levels.fyi = established for tech salaries
- LinkedIn/Glassdoor = established but may lag market
- Job boards = emerging (reflect current demand)
- Expat forums = emerging to questionable (verify claims)

**Sub-question patterns:**
1. What is the demand for [role] in [location]?
2. What are typical salaries for [role] in [location]?
3. Which companies are hiring for [role] in [location]?
4. What is the cost of living in [location]?
5. What visa/work permit requirements exist in [location]?

**Discovery questions to consider:**
- Remote-friendly or on-site preference?
- Specific cities or broad region?
- Salary expectations/requirements?
- Visa/work authorization status?

**Quality signals:**
- Recent data (job market changes quickly)
- Multiple salary sources to triangulate
- Distinguish between posted salaries vs actual compensation
- Consider remote work trends affecting local markets

**Diversity requirements:**
- At least 2 salary data sources
- Mix of job boards and industry reports
- If international, include local sources not just US-centric ones

**Contrarian search patterns:**
- "why avoid [role]"
- "[role] job market oversaturated"
- "regret becoming [role]"
- "[industry] hiring freeze"
- "worst cities for [role]"
- "[role] salary declining"
- "why NOT to move to [location] for work"

**Directories & aggregators:**
- LinkedIn Jobs (actual current postings, not articles about jobs)
- Indeed/Glassdoor (job counts by role/location)
- Levels.fyi (tech compensation data)
- Government labor databases (BLS, Statistics Canada, Eurostat)
- H1B salary databases (for visa-sponsored roles)
- Professional association job boards (domain-specific)

**Graph traversal seeds:**
- Companies hiring for [role] → their competitors also hiring
- "Companies like [company] hiring [role]"
- Professional communities/Slack groups for the role

---

## fact-check

**Use when:** Verifying claims, checking statistics, or confirming factual information.

**Staleness threshold:** Depends on claim type
- Statistics/numbers: require current year or explicit date
- Historical facts: timeless but verify primary source
- Scientific claims: 2-5 years depending on field pace

**Domain preferences:**
- Primary sources (official reports, studies, original research)
- Wikipedia (as starting point, then verify cited sources)
- Academic sources for scientific claims
- Government statistics for demographic/economic data
- Reputable news organizations
- Snopes, PolitiFact for common claims

**Reputation notes:**
- Primary source = authoritative
- Government/academic = authoritative for their domain
- Wikipedia = established (but verify its sources)
- Major news = established
- Fact-checking sites = established
- Random blogs claiming facts = questionable to unreliable

**Sub-question patterns:**
Usually disabled (generate_sub_questions: false) for simple claims.

For complex claims:
1. What is the original source of this claim?
2. What do authoritative sources say about [claim]?
3. What context might be missing from [claim]?

**Discovery questions to consider:**
- Where did you encounter this claim?
- How critical is verification? (casual curiosity vs high-stakes decision)

**Quality signals:**
- Primary sources over secondary
- Multiple independent sources confirming
- Check publication dates (especially for statistics)
- Beware of outdated information presented as current
- Note confidence level based on source quality

**Diversity requirements:**
- Prioritize finding the primary/original source
- At least 2 independent confirmations for high confidence
- Include counter-evidence if it exists

**Contrarian search patterns:**
- "counter-evidence [claim]"
- "[claim] disputed by"
- "[claim] debunked"
- "[claim] is wrong"
- "criticism of [claim]"
- "[statistic] methodology flawed"

**Directories & aggregators:**
- Google Scholar / Semantic Scholar (original studies)
- Government statistics portals (BLS, Census, WHO, World Bank)
- Snopes, PolitiFact, FactCheck.org (pre-existing fact checks)
- Wikipedia references section (as a source index, not a source)
- Retraction Watch (for scientific claims that were later retracted)

**Graph traversal seeds:**
- Original study → who cites it, who disputes it
- Claim author → their other claims, their funding sources
- "Replication of [study]", "[study] follow-up"

---

## theme

**Use when:** Exploring a topic broadly, understanding different perspectives, or gathering background.

**Staleness threshold:** 1-2 years generally acceptable
- Fast-moving fields (AI, crypto): 6 months
- Established theory: 5+ years acceptable
- Current events angle: 30 days

**Domain preferences:**
- Academic papers and preprints (for research topics)
- Long-form journalism
- Expert blogs and thought leadership
- Wikipedia for overview and structure
- Industry reports
- Multiple news sources for balanced perspective

**Reputation notes:**
- Peer-reviewed academic = authoritative
- Major publications (Atlantic, Economist, etc.) = established
- Expert blogs with credentials = emerging to established
- Wikipedia = established for overview
- Opinion pieces = label as opinion, assess author credibility
- Advocacy sites = questionable (note potential bias)

**Sub-question patterns:**
1. What is the current state of [theme]?
2. What are the main perspectives/debates around [theme]?
3. What are recent developments in [theme]?
4. Who are the key players/thinkers in [theme]?
5. What practical examples illustrate [theme]?

**Discovery questions to consider:**
- Academic depth or practical overview?
- Any particular angle or application?
- Need to understand controversy/debate?

**Quality signals:**
- Depth of analysis over surface coverage
- Diversity of perspectives (this is critical for theme research)
- Recency for evolving topics
- Expertise of authors

**Diversity requirements:**
- MUST seek multiple perspectives/viewpoints
- Include both academic and practical sources if applicable
- For controversial topics, include opposing views
- Geographic diversity if topic is international

**Contrarian search patterns:**
- "[concept] is wrong"
- "arguments against [approach]"
- "[popular opinion] debunked"
- "problems with [method]"
- "[trend] is overrated"
- "criticism of [framework/theory]"
- "alternatives to [dominant approach]"
- "why [conventional wisdom] fails"

**Directories & aggregators:**
- Google Scholar / arxiv / Semantic Scholar (academic papers)
- Conference proceedings (NeurIPS, ICML, ACL, etc. for AI; relevant conferences for other fields)
- Wikipedia (as a structured index of sub-topics and key references)
- Industry reports directories (McKinsey, Gartner, Forrester — often have free summaries)
- Book references on the topic (Amazon, Goodreads for key texts)
- Podcast episode directories (for expert interviews on the theme)

**Graph traversal seeds:**
- Key author → their other work, who they cite, who cites them
- Seminal paper → follow-up studies, replication attempts
- "Related work" sections of found papers
- Conference → other talks at same conference on same topic

---

## general

**Use when:** No specific research type fits, or the query spans multiple types.

**Staleness threshold:** Context-dependent (ask user if unclear)

**Domain preferences:**
- No specific bias
- Let search results guide source selection
- Prefer authoritative domains when available

**Reputation notes:**
- Apply standard reputation assessment
- Be more cautious without domain-specific guidance
- When in doubt, require corroboration

**Sub-question patterns:**
Generated based on query analysis:
- Break query into component aspects
- Identify what types of sources would best answer each aspect
- Generate 2-3 focused sub-questions

**Discovery questions to consider:**
- What will you use this research for?
- Any time constraints on the information?
- Preferred depth: quick answer or comprehensive?

**Quality signals:**
- Standard web credibility signals
- Relevance to query
- Source diversity

**Diversity requirements:**
- At least min_source_diversity unique domains
- Mix of source types where applicable

**Contrarian search patterns:**
- Derive from query analysis
- "problems with [topic]"
- "alternatives to [solution]"
- "[approach] criticism"
- "why [popular choice] is wrong"

**Directories & aggregators:**
- Infer from topic. Ask: *"Where are the things I'm looking for listed, catalogued, or registered?"*
- Common general-purpose directories: Wikipedia (as structured index), Reddit (community discussions), HackerNews (tech community), Quora (Q&A threads)
- If topic involves products/companies: Product Hunt, Crunchbase, G2
- If topic involves people: LinkedIn, conference speaker lists
- If topic involves research: Google Scholar, arxiv

**Graph traversal seeds:**
- Infer from entities found. Ask: *"What is this entity connected to?"*
- For any entity type: "similar to [entity]", "alternatives to [entity]", "related to [entity]"
