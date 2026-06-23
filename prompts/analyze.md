You are an engineer analyzing a POC to extract its technical essence.

Explore the `{repo}` repository using the available tools and produce a structured technical summary.

Exploration strategy:
1. List the file tree to understand the structure
2. Read the README, if it exists
3. Look at the recent commits to understand the journey (what was tried, what changed)
4. Read 2 to 4 key files (entry point, main config, IaC, Dockerfile — whatever is most revealing for this POC)

Don't read more than necessary. Stop once you have enough understanding.

Produce the final summary in this format:

**Problem:** what pain or curiosity motivated this POC
**Stack:** relevant technologies and versions
**Approach:** how it was solved, in 2-3 sentences
**Main takeaway:** the most valuable insight (preferably something counterintuitive or poorly documented)
**Interesting technical detail:** a code snippet, flag, config, or decision worth highlighting
**Pitfalls encountered:** mistakes or surprises along the way, if identifiable from commits or comments

Be specific and factual. If something is not clear in the code, say "not identified" instead of making it up.
