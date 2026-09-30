# A guided tour of the code

This document explains everything in the repo: what each piece does, why it's built that
way, and what it taught us. Read it top to bottom once. After that it works as a reference.

- [Lecture 1: The idea behind all of it](#lecture-1-the-idea-behind-all-of-it)
- [Lecture 2: The historical pipeline (`analysis/`)](#lecture-2-the-historical-pipeline-analysis)
- [Lecture 3: The first chart](#lecture-3-the-first-chart)
- [Lecture 4: The live collector (`collector/`)](#lecture-4-the-live-collector-collector)
- [Lecture 5: Infrastructure as code with CDK (`infra/`)](#lecture-5-infrastructure-as-code-with-cdk-infra)
- [Lecture 6: Repo hygiene](#lecture-6-repo-hygiene)
- [Known limitations](#known-limitations)
- [Homework](#homework)
- [Glossary](#glossary)

---

## Lecture 1: The idea behind all of it

### Stocks and flows

Think of a bank account. The **balance** is a *stock*: how much you have at a moment. The
**transactions** are *flows*: money moving in and out. If you know the balance at 9:00 and at
10:00, you know the net change for that hour exactly, even if you never saw a single
transaction.

Citi Bike works the same way, and each of our two data sources gives us one half:

| | Bank analogy | Citi Bike source | What it's good for |
|---|---|---|---|
| **Stock** | balance | Live feed (`station_status.json`): bikes at each station *right now* | Where bikes actually are, and when stations run empty or full |
| **Flow** | transactions | Monthly trip files: every ride, from A to B | Who moved bikes, and between which places |

There's a third mover that neither source records directly: **Lyft's rebalancing trucks.** A truck
changes the stock but creates no trip. So:

```
change in bikes at a station  =  (rides in − rides out)  +  truck moves
         (from snapshots)            (from trip data)        (the unknown)
```

Rearranged, **truck moves = snapshot change − trip change.** That's why we collect both
sources. Their difference is the most original story this project can tell.

### Why we can't follow individual bikes

The ideal data would be "bike #12345 was here, then there." The feed that would provide it,
`free_bike_status.json`, returns an empty list for NYC. So "migration" in this project means
**net movement between places** (stations, then neighborhoods), not individual bikes.

### Why the live collector had to start early

Trip files are published about a month later and cover years of history. The live feed only
ever shows the present. **A snapshot we don't take is gone for good.** So the plan runs two tracks:
analyze the historical trips now, and start saving live snapshots as soon as possible.

---

## Lecture 2: The historical pipeline (`analysis/`)

### The tools: DuckDB and Parquet-style thinking

August 2026 alone is **5.25 million trips** in six CSVs, about 1.9 GB. That's too big to open in
a spreadsheet but small enough for a laptop. **DuckDB** is a database that runs in-process (no
server), reads CSV and Parquet directly, and is very fast at the kind of "group by hour, sum,
average" queries analytics needs. Think of it as SQLite built for analysis. `data/trips.duckdb`
is a single file you can delete and rebuild at any time.

### `analysis/prepare.sql`: building the tables

Run it with `duckdb data/trips.duckdb < analysis/prepare.sql`. It builds five tables. Each
one answers a question the next one needs:

**1. `t`: the raw trips.** One row per ride, loaded from `data/raw/*.csv`.

> **Lesson: the bug we found.** The first version let DuckDB guess column types. DuckDB guesses
> *per file*. In one of the six files every `end_station_id` happened to look numeric, so it was
> read as a floating-point number and `5997.10` silently became `5997.1`. **About 1,100 trips
> a day lost their destination.** The symptom was subtle: in the chart, the lines didn't return to
> zero at midnight, which is impossible because riders can't destroy bikes. The fix is one line:
> declare the ID columns as `VARCHAR`. The general rule: **identifiers are text, even when they
> look like numbers.** ZIP codes, phone numbers and station IDs all break the same way.

**2. `stations`: one location per station.** Trips carry coordinates, so we take the **median**
of all reported coordinates for each station ID, across both starts and ends. We use the median
instead of the mean because e-bikes report GPS, and a few bad GPS fixes shouldn't pull a
station into the East River. Including trip *ends* matters because some stations (e.g. in Jersey
City) only appear as destinations in the NYC file.

**3. `nta` and `station_area`: stations mapped to neighborhoods.** 2,400 dots are too many for a
reader to take in, but 130 neighborhoods work. We use NYC's official **Neighborhood Tabulation Areas
(NTAs)**, which are polygons from NYC Open Data, and a *spatial join*:

```sql
LEFT JOIN nta n ON ST_Contains(n.geom, ST_Point(s.lng, s.lat))
```

This reads "match each station to the polygon that contains its point." Note the order
`(lng, lat)`: geometry uses x/y, and longitude is x. Getting that backwards is the classic bug. It
puts NYC in Antarctica. Stations outside every NYC polygon get labeled `NJ`.

**4. `events`: each trip split into two events.** This is the key modeling trick. A trip from
A to B becomes two rows:

| sid | ts | d |
|---|---|---|
| A | 08:04 | **−1** (a bike left A) |
| B | 08:33 | **+1** (a bike arrived at B) |

Once trips are events, "how did the bike count at a place change over time?" is just
`sum(d)`, grouped by place and time. Every flow calculation in this project uses this table.

**5. `hourly_net_flow`: the core migration table.** For each neighborhood and hour:
arrivals, departures, and `trip_net = arrivals − departures`. This is the table the design
doc promised, minus the truck column, which needs live snapshots.

### Sanity checks we ran, and why they matter

Before trusting any chart, we checked the results against what we'd expect:

- **Does Midtown fill in the morning?** Yes: Midtown–Times Square gains about **+1,322 bikes** on an
  average weekday between 7 and 10am.
- **Is anything suspicious?** East Village *loses* **about 1,026 bikes** in the same window, which is
  3× the next neighborhood. We broke it down by station before believing it. The loss is spread
  across many Alphabet City stations at around −45 each, with no single broken station. That fits
  reality: Alphabet City has almost no subway, so people bike to work. It's good blog material,
  and we checked it first.
- **Does everything balance?** Bikes can't be created or destroyed, so over a full day the
  zones should add up to about zero. This check caught the ID bug. What remains, about −470/day,
  matches the **476 trips/day that have no end station** (lost, stolen, or ended by support).

The habit to take from this: **every surprising number is either a story or a bug. Find out
which before you publish.**

---

## Lecture 3: The first chart

### `analysis/tide_chart.py`

**Output:** `output/tide_2026-08.png` (16:9, sized for a slide) and `output/tide_2026-08.csv`
(the exact numbers, so you can rebuild the chart natively in PowerPoint).

**The question it answers:** over an average day, how many bikes have riders moved into or out
of each part of the city?

**How the query works**, one CTE (named sub-query) at a time:

1. **`role`**: sorts every neighborhood into one of three zones. A Manhattan neighborhood is
   `fills` if it gains bikes on weekday mornings and `empties` if it loses them. Everything else
   in NYC is `outer`. NJ is excluded because its trips starting in Jersey City are in a separate
   file we haven't loaded.
   *Why data-driven zones instead of "above/below 59th St" (the original design)?* Because the
   line is not geographic. East Village is below 59th and empties heavily, while the hospital area of the
   Upper East Side fills. We named the zones for what they *do* so the legend can't be wrong.
2. **`q`**: net bikes per zone per 15-minute slot per day.
3. **`grid`**: an empty skeleton of every zone × day × slot. **Why?** A slot with no trips has no row, and a
   running sum over missing rows goes wrong. The grid fills those gaps with zeros.
4. **`cum`**: a *window function*, `sum(...) OVER (PARTITION BY zone, day ORDER BY slot)`, gives
   the running total from midnight within each day.
5. **Final select**: averages the running totals across days, split into weekday and weekend.

**The design choices, and the rule behind each:**

| Choice | Rule |
|---|---|
| The title states the finding ("~5,000 bikes…"), not the topic ("Bike flows by zone") | A slide should make its point even if nobody reads the axis |
| Two panels (weekday vs. weekend) with the **same y-axis** | Shared scale makes "weekends are calmer" visible without reading numbers |
| Three lines, fixed colors, peak values labeled directly | Readers shouldn't have to look back and forth to the legend; ≤ 3 series keeps colors distinct for color-blind readers |
| Colors from a validated palette, not matplotlib defaults | The palette is checked for color-blind separation and contrast |
| Subtitle says "Truck rebalancing not included" and names the source | Honesty about what the data *can't* show |

**What it shows:** on weekdays, the areas that fill peak at **+5,447** bikes around noon, the areas that empty
bottom out at **−4,350**, and the outer boroughs at **−2,591**. All three return to near zero
overnight. Weekends are flatter and later.

---

## Lecture 4: The live collector (`collector/`)

### `collector/poll_local.py`: an experiment, not production

A throwaway script that saves the live feed every 60 seconds to `data/live/`. It exists for one
reason: to *measure* how much information a 5- or 10-minute snapshot interval loses, by collecting at
1 minute and thinning it. It runs overnight into the 9/30 morning rush. Deciding this with real
data beats guessing.

### What the interval math found

(Full table in `DESIGN.md`.) The short version:

- **Cost doesn't decide it.** Even 1-minute snapshots cost under $0.50/month.
- **Hourly neighborhood totals are exact at any interval that divides 60**, by the bank-balance
  argument in Lecture 1. That also explains the CDK scheduling fix in Lecture 5.
- **Detail within the hour is what you lose.** At a single station, only 62% of movement is visible at
  5 min and 27% at 60 min, because an arrival and a departure between two snapshots cancel out.
- **Still open:** how many short empty or full stretches a 5–10 minute snapshot misses.

### `collector/snapshot/handler.py`: the production Lambda

About 30 lines. AWS calls `handler(event, context)` on a schedule. It:

1. Reads which feed to fetch from the event (`station_status` or `station_information`),
   **rejecting anything else**. The function should never be tricked into fetching arbitrary URLs.
2. Downloads the feed.
3. Saves it to S3, **gzipped and unmodified**, at:
   ```
   raw/station_status/dt=2026-09-30/station_status_20260930T120014Z.json.gz
   ```

The design decisions packed into those lines:

- **Store raw, transform later.** We save exactly what the feed returned. If our
  processing turns out to be wrong (see the station ID bug), we can reprocess. If we'd saved only
  our processed version, the mistake would be permanent.
- **The file name uses the feed's own timestamp** (`last_updated`), not the Lambda's clock. So if
  AWS retries a run, or the feed hasn't changed since the last run, the write lands on the *same* key
  and overwrites instead of creating a duplicate. This property is called **idempotency**: doing it
  twice has the same effect as doing it once. Any job that can be retried should have it.
- **`dt=YYYY-MM-DD/` folders** are *Hive-style partitions*. Tools like Athena and DuckDB
  understand them, so a query for one week reads only that week's files.
- **Times are UTC.** Servers should store UTC and convert to New York time only for display.
  Daylight saving time makes local timestamps ambiguous once a year.
- **No `Content-Encoding: gzip` header.** With that header, some clients silently decompress on
  download and others don't. Stored as a plain `.gz` file, every reader gets the same bytes.
- **Only standard library + `boto3`**, which the Lambda runtime already includes. No dependencies
  means nothing to package and nothing to patch.

---

## Lecture 5: Infrastructure as code with CDK (`infra/`)

### The concepts, in the order they matter

**Infrastructure as code** means defining cloud resources in source files instead of clicking
through the console. You get reviewable diffs, git history, and the ability to rebuild everything.

**CloudFormation** is AWS's native way to do this: a long YAML/JSON template describing
resources. It works, but writing it by hand is verbose.

**CDK** lets you write TypeScript that *generates* CloudFormation. You get loops, types,
autocomplete, and high-level building blocks. The workflow:

```
your .ts code ──cdk synth──> CloudFormation template ──cdk deploy──> real AWS resources
                                     │
                                  cdk diff  (compare the template with what's deployed; changes nothing)
```

The building blocks are nested:

- **App**: the whole program (`bin/infra.ts`).
- **Stack**: one deployable unit, which becomes one CloudFormation stack. Ours is `CitibikeCollector`.
- **Construct**: any piece inside a stack. `new s3.Bucket(...)` is a construct. These are called
  **L2 constructs**: high-level classes that fill in sensible defaults and generate the right
  IAM policies for you.

**Bootstrap** (`cdk bootstrap`) is a one-time setup per account and region that creates a staging
bucket and the deploy roles. Your account already had it from the volo project.

### `infra/bin/infra.ts`: the entry point

- Creates the App and one `CollectorStack`.
- **Pins `env` to account `404933715334` / `us-east-1`.** Without it, the stack deploys to whatever
  account your terminal happens to be logged into, which is a real risk once you have more than one.
- Reads **`snapshotMinutes` from CDK context** (default 5). You can try another interval without
  editing code: `npx cdk diff -c snapshotMinutes=10`.
- Tags every resource with `project=citibike-migration`, so the AWS bill can be filtered to this project.

### `infra/lib/collector-stack.ts`: the stack

Four resources, each with one reason to exist:

**The S3 bucket**
```ts
blockPublicAccess: BLOCK_ALL, encryption: S3_MANAGED, enforceSSL: true,
removalPolicy: cdk.RemovalPolicy.RETAIN,
```
The first three are baseline security: private, encrypted at rest, and HTTPS only. **`RETAIN`** is the
important one. By default, deleting a stack deletes its resources. Code can be redeployed, but
weeks of snapshots can't be re-collected. `RETAIN` keeps the bucket even through `cdk destroy`.

**The Lambda function**
- `Code.fromAsset('../../collector/snapshot')`: CDK zips that folder and uploads it at deploy
  time. The Python lives with the rest of the collector code, not inside `infra/`.
- `ARM_64`: Graviton processors are about 20% cheaper for the same work, and our code doesn't care about CPU type.
- `memorySize: 256`: Lambda gives CPU in proportion to memory, so 256 MB decompresses a 1 MB
  JSON quickly. It costs fractions of a cent either way.
- A log group with **30-day retention**. The default keeps logs forever, a small cost that grows
  without anyone noticing.

**Least privilege: `bucket.grantPut(snapshot)`**
One line that generates an IAM policy allowing the function to **write objects, and nothing else.**
It can't read, list or delete. If the function were ever compromised, the worst it could do is
write junk. It could never erase your data. The `cdk diff` output showed exactly these permissions,
which is why reading that diff before deploying is worth the time.

**The two schedules (EventBridge rules)**
- `station_status` on `cron(0/5 * * * ? *)`, i.e. :00, :05, :10…
- `station_information` daily at 08:00 UTC (4am New York), because stations get added and moved.

> **Lesson: `rate()` vs `cron()`.** The first draft used `Schedule.rate(5 minutes)`. A rate
> schedule counts from *when you deploy*, so snapshots would land at :03, :08, :13… and never at
> :00. Hourly totals are exact only with a snapshot *on* each hour boundary (Lecture 1), so
> a rate schedule would have quietly broken our main guarantee. `cron` pins to the clock. The stack also
> **refuses** intervals that don't divide 60 (e.g. 7 minutes), and a test checks that.

`retryAttempts: 2` means that if an invocation fails, EventBridge tries again. That's safe *because* the handler is
idempotent (Lecture 4). The two properties work as a pair.

### `infra/test/infra.test.ts`: tests for infrastructure

This looks odd at first: why test config? Because the most expensive mistakes here are *config*
mistakes. These tests synthesize the stack in memory (no AWS needed) and assert on the template:

1. The schedule is clock-aligned (`cron(0/5 …)`).
2. The bucket has `DeletionPolicy: Retain`.
3. The function's permissions are write-only.
4. A 7-minute interval is rejected.

Each test protects a guarantee from an earlier lecture. If a future edit breaks one of them,
`npm test` fails before anything reaches AWS.

### `infra/cdk.json`

- `"app": "npx tsc && npx tsx bin/infra.ts"`: how CDK runs your program. It type-checks, then executes.
- `"profile": "personal"`: every `cdk` command uses your IAM-user login automatically.
- The long `context` block is **feature flags** CDK adds at `init`. They pin behavior to
  modern defaults. Leave them alone.

### Commands you'll use (run inside `infra/`)

| Command | What it does | Touches AWS? |
|---|---|---|
| `npm test` | Run the stack tests | No |
| `npx cdk synth` | Print the generated CloudFormation | No |
| `npx cdk diff` | Compare the template with what's deployed | Reads only (uploads the template to the staging bucket) |
| `npx cdk deploy` | Create or update the real resources | **Yes**. Shows IAM changes and asks for confirmation. |
| `npx cdk destroy` | Delete the stack (the bucket is **kept**) | **Yes** |

---

## Lecture 6: Repo hygiene

- **`data/` is gitignored.** 1.9 GB of CSVs doesn't belong in git, and anyone can re-download
  it (the README has the exact commands). Rule: **commit code and small results; recreate large
  inputs from their source.**
- **`output/` is committed.** Charts and their CSVs are the product, and they're small.
- **`.venv/` and `node_modules/` are gitignored.** They're rebuilt from `pip install` and `npm install`.
- **`DESIGN.md`** is the plan and the decisions, with the reasons behind them. **`README.md`** is how to run things.
  This file explains how the code works. Each has one job.

---

## Known limitations

Things that are true today and worth knowing before publishing anything:

1. **Zones are defined from the same data they describe.** "Areas that fill on weekday
   mornings" is decided from August and then plotted on August. That's fine for describing
   a pattern, but it's circular if presented as a *prediction*.
2. **One month, in summer.** August has vacations and good weather. Winter will look different.
3. **Trips with no end station** (about 476/day) are counted as departures only.
4. **Jersey City is half-loaded.** Trips starting there are in separate `JC-` files.
5. **"Snapshot at :00" means about :00.** The Lambda runs within seconds of the minute, and the feed
   itself can be up to 60 s old. That's fine for hourly totals. It's worth remembering when
   comparing with trip timestamps.
6. **Timestamps don't match yet:** trip files are New York local time, while snapshots are UTC.
   The first analysis that joins them must convert one to the other.

---

## Homework

Short exercises to build CDK intuition, all safe and none of them deploy:

1. `cd infra && npx cdk synth > /tmp/t.yaml`. Find the IAM policy in the output and compare it
   with the one line `bucket.grantPut(snapshot)`. That comparison shows what L2 constructs save you.
2. Run `npx cdk synth -c snapshotMinutes=10` and compare with the default. What changes? Only the schedule
   expression should. (After the first deploy, `cdk diff -c snapshotMinutes=10` shows the same thing against live AWS.)
3. Change `grantPut` to `grantReadWrite` and run `npm test`. Which test fails, and why is
   that the right outcome? (Revert afterward.)
4. Add a test asserting the log group's `RetentionInDays` is 30.
5. Harder: add a CloudWatch alarm that fires if the function errors 3 times in 15 minutes
   (`snapshot.metricErrors()` → `new cloudwatch.Alarm(...)`). This is the next real feature: it
   tells us if collection silently stops.

---

## Glossary

| Term | Meaning |
|---|---|
| **GBFS** | General Bikeshare Feed Specification, the standard JSON format bike-share systems publish |
| **NTA** | Neighborhood Tabulation Area, NYC's official neighborhood polygons |
| **Stock / flow** | A quantity at a moment (bikes at a station) vs. a movement over time (rides) |
| **Net flow** | Arrivals − departures. Positive means a place is filling. |
| **Rebalancing** | Lyft moving bikes by truck or van. Visible only as snapshot change − trip change. |
| **Idempotent** | Running it twice has the same effect as running it once |
| **Hive partition** | A `key=value/` folder in S3 that query tools use to skip irrelevant files |
| **Construct (L2)** | A high-level CDK class, e.g. `s3.Bucket`, that generates correct low-level resources |
| **Synth** | Turning CDK code into a CloudFormation template |
| **Least privilege** | Giving each component only the permissions its job requires |
