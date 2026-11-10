# Research: does the design make parallel agents less draining?

A review of the concepts in DESIGN.md, done on 2026-11-09 and 2026-11-10. It tests the design's bets against three sources:

1. my own Claude Code transcripts;
2. research on interruptions, resumption and the supervision of automation;
3. the tools that already exist.

The ideas that came out of it are at the end, with their status in DESIGN.md.

## Summary

- **The design lowers the cost of each item but does not limit their number.** It makes each item cheaper: reading it (digest, kind), finding the next one (inbox, priority) and closing it. In my data, the main problem is the number of items: I keep about twice as many sessions in play as I can serve, so the extra ones wait as open loops. The design has nothing that limits that number.
- **A good inbox may even raise that number.** In the one lab study of a dashboard for parallel coding agents (ParallelPilot), people ran more agents at once with it.
- **Reading is my main cost, and the digest addresses it.** My response time grows with the length of the reply.
- **Most replies need a review, not an answer.** Only 13% of replies ask me something. The design leaves review (diffs, evidence) for later.
- **Claude Code now ships Agent View,** which covers the status layer of the inbox. What stays specific to Brain Fry:
  - the kind label;
  - my manual priority;
  - a record that outlives the deletion of transcripts;
  - the close step;
  - one window across all projects.

## 1. My transcripts

### Data and definitions

- Source: `~/.claude/projects/*/*.jsonl`, read on 2026-11-09.
  - Only the main transcripts: subagent transcripts (in subfolders) and `isSidechain` entries are left out.
  - 296 sessions with at least one typed prompt, 1,217 typed prompts, 39 active days from 2026-08-27 to 2026-11-09, 19 project folders.
  - 295 of 297 sessions came from the VS Code panel. So this describes how I work in VS Code, not in Brain Fry.
- **Typed prompt:** a `user` entry with `origin.kind` = `human`, not a compaction summary.
- **Turn:** starts at a typed prompt or at a background task notification (`origin.kind` = `task-notification`). Its **Stop** is the last `assistant` entry before the next turn starts. 1,454 turns: 1,198 started by me, 256 by a background task.
- **Agent turn:** from the prompt to the Stop. It includes the time blocked on a permission prompt.
- **Response time:** from the Stop to my next typed prompt in that session. For comparisons, times over 2 h are dropped as breaks.
- **Active minute:** a minute with a typed prompt less than 10 min away, before or after. 7,835 active minutes, about 130 h.
- **In play:** a session that is running (from the prompt to the Stop), or waiting on me (from the Stop to my next prompt, for at most 30 min).
- **Switch:** two consecutive typed prompts, less than 30 min apart, that go to different sessions.
- **Stay and switch, compared:**
  - Stay: my previous prompt went to the same session, before its Stop. Measured from the Stop.
  - Switch: my previous prompt went to another session, after this session's Stop, so the reply was already waiting. Measured from that previous prompt.
- **Question:** a `?` in the last paragraph of the final reply. **Bare approval:** a next prompt that is only "yes", "ok", "go", "continue", or similar. **Correction:** a next prompt that starts with "no", "wrong", "still", "again", "I said", "hmm", or similar. All three are crude text rules.
- The scripts were throwaway and were not kept. The definitions above are enough to rerun the analysis.

### Results

| Measure | Value |
|---|---|
| Agent turn | median 2.1 min; 25% under 49 s, 90% under 12 min |
| Tool calls per turn | median 5, 90% at most 27 |
| My response time | median 6.6 min; 43% under 5 min, 79% under 30 min, 7% over 8 h |
| Typed prompts per active day | median 21 (quartiles 11 and 42) |
| New sessions per active day | about 7.6 |
| Typed prompts per session | median 2; a quarter have 1; 90% have at most 9 |
| Sessions resumed after more than a day | 26 of 296 (9%); after more than 4 h: 55 (19%) |
| Active minutes with 3 or more sessions in play | 61% (5 or more: 23%) |
| Active minutes with 2 or more agents running | 44% |
| Active minutes with at least one agent waiting on me | 77% |
| Active minutes in which I wait for agents (one runs, none waits) | 23% |
| Active hours in which I prompted 3 or more sessions | 48% |
| Consecutive prompts that go to another session | 75% (1,037 pairs) |
| Time with 2 or more agents running in which two share a project folder | 67% |
| Stops followed by a background task notification, not by me | 267 of 1,454 (18%) |
| Final replies with a question in their last paragraph | 13% (ending with `?`: 7%) |
| Next prompts that are a bare approval | 2% |
| Interrupts (Esc) | 15 of 1,198 turns; plus 82 tool calls that I rejected |
| Final reply length | median 1,950 characters; a quarter over 3,100; 10% over 4,700 |

On 2026-11-09, `claude agents --json` (Agent View) listed 9 live sessions in 5 folders: 8 idle, 1 busy.

### Findings

1. **The bottleneck is me, not the agents.**
   - The fan-out formula from supervisory control (Olsen & Goodrich) estimates how many agents one person can keep busy. Fan-out = NT / IT + 1:
     - NT is the time that an agent runs on its own: the agent turn, mean 302 s (capped at 1 h).
     - IT is my handling time per turn: the response time in stay cases under 30 min, mean 309 s.
   - That gives a fan-out of 2.0 with means and 1.7 with medians.
   - IT includes pauses, so it overestimates my handling time, and the true fan-out is a bit higher. But it is not 5.
   - With 3 or more sessions in play 61% of the time, the extra sessions wait. That matches "at least one agent waits on me in 77% of active minutes".
2. **Reading is my main cost.** In stay cases, my response time grows with the length of the reply:

   | Reply length (characters) | Median response time | n |
   |---|---|---|
   | under 500 | 128 s | 30 |
   | 500 to 1,500 | 120 s | 96 |
   | 1,500 to 3,000 | 231 s | 129 |
   | 3,000 and more | 347 s | 87 |

   One caveat: long replies may also be the ones about hard problems, which need thinking time anyway.
3. **Few replies ask me anything.** Only 13% of replies have a question at the end, and only 2% of my answers are a bare approval. Most items will be "review" or "fyi", so the expensive step is review.
4. **18% of Stops are false alarms.** After these Stops, the agent continued on its own when a background task finished. The design marks every Stop green and puts the task in the inbox. The Stop payload has `background_tasks`; when it is not empty, the task should stay "working". This is not yet in DESIGN.md.
5. **Most tasks are short.** The rolling digest and thread navigation help the long minority. For most tasks, the last reply is the whole context. About 7.6 new sessions per active day make archiving by hand a daily chore.
6. **Parallel agents usually share a repo.** A diff per task needs one worktree per task. Caveat (2026-11-10): sharing a folder measures how often the risk was present, not how often agents broke each other's work. DESIGN_DETAILS.md, "Worktrees", proposes a count of collisions (same file edited by two sessions within about 30 minutes) before worktrees become a default.
7. **No sign of degradation with load.** With 0 to 5 other sessions in play when a reply arrived, three measures stay flat:

   | Other sessions in play | Median response time | Corrections | Prompts of 3 words or fewer | Median prompt length | n |
   |---|---|---|---|---|---|
   | 0 | 379 s | 2% | 9% | 227 | 99 |
   | 1 | 354 s | 2% | 7% | 200 | 134 |
   | 2 | 259 s | 4% | 9% | 164 | 184 |
   | 3 | 394 s | 2% | 8% | 296 | 182 |
   | 4 | 399 s | 1% | 8% | 261 | 122 |
   | 5 or more | 256 s | 0% | 6% | 280 | 83 |

   Switching shows the same picture. I took 205 s to answer a waiting reply after a switch, and 217 s after staying in the same session. The replies were even longer in the switch cases (median 2,640 characters, against 1,820).

### What the data cannot show

The flat table in finding 7 does not show that parallel work is free:

- **Nobody assigned the number of sessions.** I opened more sessions when I chose to, probably when I had capacity, or when the tasks were easy. Suppose I only open 5 sessions when the work is easy. Then hard work never appears in the "5 sessions" row, and a real effect of load stays hidden.
- **The measures are crude.** Drain may show up as tiredness in the evening, or as reviews that go less deep. Transcripts record neither.
- **The cost of a switch may be felt rather than timed.** Equal response times after a switch and after a stay do not mean that a switch costs nothing.

So the transcripts show the conditions (many sessions in play, frequent switches, me as the bottleneck, reading as the main cost). They do not measure the drain itself. Measuring it needs the plan in idea 10.

## 2. Research on each bet

Labels: RCT = randomized experiment; lab; field = observed in real work; survey = self-report at one point in time; preprint = not peer reviewed.

### Studies about AI work

- **BCG, "AI brain fry"** (Bedard et al., HBR, March 2026). Survey, n = 1,488 US workers. https://hbr.org/2026/03/when-using-ai-leads-to-brain-fry
  - 14% reported fatigue from using or overseeing AI.
  - The strongest predictor was tools that need continuous monitoring.
  - Perceived productivity rose up to 3 tools used at once, then fell.
  - The brain-fry group reported more decision fatigue and more errors.
  - Everything is self-reported at one point in time, so this is an association, not a cause.
  - I read the figures through BCG's summary and secondary coverage (the HBR article is behind a paywall).
- **METR 2025.** RCT, 16 experienced open-source developers, 246 tasks. https://arxiv.org/abs/2507.09089
  - Tasks with AI took 19% longer (95% CI +2% to +39%), while the developers believed that AI made them 20% faster. So felt productivity is unreliable.
  - METR's update of February 2026 says that time measurement fails for developers who run several agents and do other work while waiting. https://metr.org/blog/2026-02-24-uplift-update/
- **ParallelPilot** (arXiv 2609.33113, September 2026). Preprint: interviews (n = 14), then a lab study (n = 16). I did not open it myself. https://arxiv.org/abs/2609.33113
  - The most common pattern was 3 parallel sessions; all participants reported monitoring overhead; 7 of 14 kept their own markdown logs.
  - An ambient dashboard with one-line summaries raised tickets per minute by 63%, and the peak number of agents went from 2.3 to 3.25.
  - With the dashboard, participants inspected the code less deeply ("giving up my internal representation").
- **Anthropic, three reports.**
  - December 2025 (survey n = 132, plus interviews): engineers keep several instances running, but fully delegate only 0 to 20% of their work. The report calls this the "paradox of supervision". https://www.anthropic.com/research/how-ai-is-transforming-work-at-anthropic
  - February 2026 (usage logs): with experience, auto-approve rises from about 20% to over 40% of sessions. https://www.anthropic.com/research/measuring-agent-autonomy
  - January 2026 (RCT, n ≈ 52, learning a new library): quiz score 50% with AI against 67% without; the largest gap was on debugging. https://www.anthropic.com/research/AI-assistance-coding-skills
- **Ye et al. 2026** (preprint, 107 developers): 94% did not notice sabotage that an agent had inserted. https://arxiv.org/abs/2606.05647

### Verdicts

| Bet in DESIGN.md | Verdict | Evidence |
|---|---|---|
| Write every open task down (task file, digest) | The principle is supported, the mechanism is different | **For:** Masicampo & Baumeister 2011 (lab): making a specific plan removed the intrusive thoughts that unfinished goals cause. https://pubmed.ncbi.nlm.nih.gov/21688924/ **But:** the person writes a plan for what comes next, while a digest summarizes the past. And Storm & Stone 2015 (lab): offloading frees memory only when the store is trusted. The Zeigarnik memory effect does not replicate (Ghibellini & Meier 2025, meta-analysis, https://doi.org/10.1057/s41599-025-05000-w); the urge to resume does |
| Pull, no notifications | Mixed | **Interruptions cost:** Mark et al. 2008 (lab, n = 48): more stress and effort, https://www.interruptions.net/literature/Mark-CHI08.pdf. Iqbal & Horvitz 2007 (field, n = 27): 10 to 25 min to get back to the task, https://www.erichorvitz.com/CHI_2007_Iqbal_Horvitz.pdf. **But removing every signal also costs:** Fitz et al. 2019 (field RCT, n = 237): notifications batched 3 times a day were best, and the group with none felt more anxious, https://doi.org/10.1016/j.chb.2019.07.016. Iqbal & Horvitz 2010: with notifications off, people interrupted themselves to check. In Dev mode the inbox is always on screen, so it already works as a passive signal. The risk is compulsive checking |
| Close step (answer, dismiss, archive) | Partly supported | Leroy 2009 (lab): attention stays on an unfinished task and lowers performance on the next one, https://econpapers.repec.org/RePEc:eee:jobhdp:v:109:y:2009:i:2:p:168-181. Leroy & Glomb 2018 (4 studies): a 1-minute "ready to resume" plan reduced that, https://econpapers.repec.org/RePEc:inm:ororsc:v:29:y:2018:i:3:p:380-397. The benefit comes from the plan, not from removing an item from a list |
| AI summaries for resuming | Mixed | **Cues help:** Parnin & DeLine 2010 (lab, n = 15 developers), https://www.microsoft.com/en-us/research/publication/evaluating-cues-for-resuming-interrupted-programming-tasks/. **Over-trust is well documented:** complacency under multitask load, in experts too (Parasuraman & Manzey 2010, review, https://pubmed.ncbi.nlm.nih.gov/21077562/); shallower inspection (ParallelPilot); missed sabotage (Ye 2026). PRs with AI-written descriptions were merged faster (Xiao et al. 2024, 18,256 PRs, https://arxiv.org/abs/2402.08967), which shows faster acceptance, not better review |
| Kind label (review, answer, rule, FYI) | No direct evidence | No other tool does it. Wickens & Dixon 2007 (synthesis): diagnostic aids that are right less than about 70% of the time do worse than no aid, https://scilit.com/publications/9eef420e7b307afbf03cc65cccfa2c0e |
| Red first | Supported, for another reason | In queueing theory, serving quick items first lowers the total waiting cost, and a permission prompt is quick to handle. An idle agent costs nothing; my time is what counts |
| Number of agents per person | Small, about 3 | Olsen & Wood 2004 (lab): fan-out from 1.5 to 9, depending on how long the robots run on their own; handling time grows with the number of robots, https://hci.rwth-aachen.de/materials/conferences/CHI2004/1p231.pdf. Cummings & Guerlain 2007 (lab, n = 42): performance drops when operators are busy more than about 70% of the time, https://scholars.duke.edu/publication/1108373. Cummings et al. 2013: too little load also hurts. BCG: 3 tools. ParallelPilot: 3 sessions |
| Board with priority columns, the later ones hidden | No evidence about strain | Little's law: at a fixed throughput, fewer items in progress means a shorter wait for each. The design hides items but does not limit them. Kanban studies are mostly experience reports (Ahmad et al. 2018, https://lero.ie/bibliography/kanban-in-software-engineering-a-systematic-mapping-study/) |

The research agent could not verify every detail:
- the number of participants in each study of Masicampo & Baumeister;
- the BCG figures (secondary coverage only);
- Katidioti 2016 (abstract only);
- the monitor numbers in Ye 2026 (the sources disagree).

## 3. Existing tools

Surveyed on 2026-11-09 by a research agent. I checked the Claude Code facts myself with the CLI (2.1.319) and the hooks documentation.

- **Agent View in Claude Code** (`claude agents`, research preview; checked in the CLI). https://code.claude.com/docs/en/agent-view
  - It groups sessions by state (needs input, working, completed, ...), shows a Haiku summary at each turn end and a waiting timer, and can dispatch, fork and stop idle sessions.
  - `claude agents --json` prints the live sessions as JSON, VS Code ones included. Fields: `pid`, `cwd`, `kind`, `sessionId`, `status`, `name`, `startedAt`.
- **Claude desktop app and Claude Projects.** A status sidebar, a diff with line comments sent as one batch, and automatic archive (when a PR merges, or after a week without activity). Projects groups threads by what they need: ready for review, waiting on you, working. It runs in the cloud first and is in beta. https://code.claude.com/docs/en/desktop and https://code.claude.com/docs/en/claude-projects
- **Others:**
  - Codex inside the ChatGPT app: a "Triage" inbox; it archives runs that found nothing.
  - Cursor 3: an agents window.
  - GitHub: an agents panel.
  - Nimbalyst (ex-Crystal): a kanban, and the list of files that each session read or wrote.
  - Vibe Kanban: a worktree per task.
  - Claude Squad, CCManager, Agent Deck, cmux: state detection, or an unread badge.
- **The field is shrinking:**
  - Terragon closed in January 2026, and Crystal was deprecated in February.
  - Vibe Kanban's company closed in April; the open-source repo continues.
  - Conductor is joining Meta.
  - Jules shuts down on 2026-12-31.
- **No tool labels a reply by the kind of decision that it needs.**
- **What users report:**
  - A limit of 2 to 3 parallel threads is common: "The lingering open tabs in our heads are the heaviest load, not the switch itself" (Hacker News, https://hn.svelte.dev/item/47797632).
  - Osmani scopes each thread so that its output can be reviewed in about 30 min, with a ceiling of 3 to 4 threads (https://addyosmani.com/blog/cognitive-parallel-agents/).
  - A notification without the question still forces you to read the transcript (Wirth, Nimbalyst, a vendor: https://dev.to/stravukarl/what-actually-breaks-when-you-run-5-claude-code-agents-in-parallel-1lbd).

What this means for Brain Fry:

- **Possible conflict, not checked by me.** Agent View is on by default. The left arrow on an empty prompt would open it inside Brain Fry's terminal, and `/fork` creates a background session that Brain Fry does not own. Setting `leftArrowOpensAgents: false` for the child sessions should avoid that.
- **`claude agents --json`** could set the `live` flag, which would replace `owned.json`.
- **Hook events not used yet** (documented; checked by a web-fetch agent):
  - `PermissionRequest`: fires when a tool call needs a permission decision, so a task could turn red at once instead of after about 6 s.
  - `StopFailure`: fires when a turn ends on an API error. Today such a task stays orange.
- **Worth borrowing:**
  - the time that each red item has waited;
  - the full question in the inbox row;
  - automatic archive after N idle days;
  - the files that each task read or wrote;
  - Brain Fry's title as the session title in Claude Code. The SessionStart payload has `session_title`; whether a hook can set it is unchecked.

## 4. Ideas, and their status

1. **Limit the number of open items.** Show "N agents waiting on you". When 3 are waiting, a new task asks me to close one first. This is the strongest finding in my data and in the research. Status: open.
2. **Make agents need me less often.** By the fan-out formula, doubling the time that an agent runs without me helps as much as halving my reading time, and a median turn of 2 min is short. Ways to get longer turns: a short brief at the start of a task (goal, when it is done, what the agent may decide alone), and auto mode. Status: open, mostly a habit, not a feature.
3. **Reply trailer.** The agent ends each reply with `[needs: KIND] TEXT`. Status: specified in DESIGN_DETAILS.md (Reply trailer), not built.
4. **Rulings become rules.** When a thread has the kind "rule", offer to add my answer to the project's CLAUDE.md or to a decisions file, so that the same question does not come back. That is the "endless loops" problem in brain_fry.md. Status: open.
5. **Auto-advance.** After I answer, Brain Fry selects the next inbox item. Status: built (DESIGN_DETAILS.md, Auto-advance).
6. **Resume note.** One line from me when I dismiss or defer a task. Status: specified in DESIGN_DETAILS.md (Resume note), not built.
7. **Review with a diff and evidence, for each thread,** with a worktree per task. Status: v2 in DESIGN_DETAILS.md.
8. **Keep the status honest.**
   - A Stop with background tasks running is not "needs me" (finding 4).
   - Tasks that are idle for N days and not in the inbox are archived automatically.

   Status: open.
9. **Check the kind labels.** Label about 50 threads myself and compare with Sonnet's labels. Status: partly covered, because the trailer spec keeps Sonnet's kind when it disagrees with the agent's.
10. **Measure whether Brain Fry helps.**
    - **What to measure.** Brain Fry logs every event, so it can compute the measures above every week: fan-out, items waiting, time from Stop to my answer, share of switches. Add one question at the end of each day: "How drained am I, 1 to 7?". The transcripts above give the "before".
    - **Why not a simple before/after comparison.** It cannot show that Brain Fry caused a change, because tasks, deadlines and habits change at the same time. A new tool also feels better for a few weeks.
    - **Alternate instead.** For example, one week with Brain Fry and one week without, several times over. Alternating spreads those changes over both conditions.
    - **Allow a few weeks of each.** The rating of a single day varies a lot, so only a few weeks of data can make a difference stand out from that day-to-day variation.

    Status: open.
