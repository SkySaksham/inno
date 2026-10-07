import React, { useCallback, useEffect, useState } from 'react';
import {
  CursorPointer01Icon,
  Layers01Icon,
  Notification03Icon,
  Settings02Icon,
} from '@hugeicons/core-free-icons';
import { HugeiconsIcon } from '@hugeicons/react';
import Hyperspeed from './components/Hyperspeed.jsx';
import FoldText from './components/FoldText.jsx';
import ScrollExpand from './components/ScrollExpand.jsx';
import ScrollStack, { ScrollStackItem } from './components/ScrollStack.jsx';
import BranchedMenu from './components/BranchedMenu.jsx';

// Set this to the deployed metrics-backend /metrics/summary endpoint.
const METRICS_API_URL = 'https://inno-mv5y.onrender.com/metrics/summary';
const HERO_HYPERSPEED_OPTIONS = {
  colors: {
    roadColor: 0x111713,
    islandColor: 0x171e19,
    background: 0x101511,
    shoulderLines: 0x829d75,
    brokenLines: 0xa8c79c,
    leftCars: [0x426e4c, 0x83bb63, 0xa1d16f],
    rightCars: [0x7eaa5a, 0xa0cb7b, 0x587a68],
    sticks: 0xc9f36a,
  },
};

const WORKFLOW_ITEMS = [{ label: 'Review pipeline', children: [
  { value: 'metadata', label: 'Repository map', icon: Layers01Icon },
  { value: 'review', label: 'PR review', icon: Notification03Icon },
  { value: 'scan', label: 'Scan and understand', icon: Settings02Icon },
  { value: 'findings', label: 'Ranked findings', icon: Layers01Icon },
  { value: 'fix', label: 'Fix and verify', icon: CursorPointer01Icon },
] }];

const WORKFLOW_CARDS = {
  metadata: { title: 'Metadata stays current', text: 'Each commit updates repository metadata and call graphs.' },
  review: { title: 'Every PR triggers a review', text: 'INNO checks each pull request in your existing workflow.' },
  scan: { title: 'Scan code and intent', text: 'Linters and Bandit catch known issues; Copilot reads the change intent.' },
  findings: { title: 'Rank findings by severity', text: 'AI uses exact repository context to suggest fixes or recommend closing an unsafe PR.' },
  fix: { title: 'One tap. Re-scan. Merge.', text: 'Apply a fix, rerun linters, then merge. Metadata and all-time stats update again.' },
};

function Icon({ icon, size = 18 }) {
  return <HugeiconsIcon icon={icon} size={size} strokeWidth={1.8} />;
}

function FoldHeading({ text, color = 'inherit', trigger = 'scroll', onComplete }) {
  return (
    <FoldText
      text={text}
      splitBy="word"
      hinge="top"
      trigger={trigger}
      duration={1.05}
      stagger={0.075}
      ease="power3.out"
      perspective={700}
      creaseShading={0.4}
      fontSize="inherit"
      fontWeight="inherit"
      color={color}
      onComplete={onComplete}
    />
  );
}

function MetricsPanel() {
  const [metrics, setMetrics] = useState(null);
  const [state, setState] = useState('loading');

  async function loadMetrics() {
    setState('loading');
    try {
      const response = await fetch(METRICS_API_URL, { headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error(`Metrics API returned ${response.status}`);
      setMetrics(await response.json());
      setState('success');
    } catch (error) {
      console.error('Could not load Inno metrics:', error);
      setState('error');
    }
  }

  useEffect(() => { loadMetrics(); }, []);

  const count = value => new Intl.NumberFormat().format(Number(value) || 0);
  const percent = value => {
    const number = Number(value);
    return Number.isFinite(number) ? `${number.toFixed(1)}%` : 'N/A';
  };
  const values = metrics || {};
  const rows = [
    { icon: Settings02Icon, tone: 'mint', label: 'Findings reviewed', value: count(values.total_findings) },
    { icon: Notification03Icon, tone: 'amber', label: 'False-positive rate', value: percent(values.false_positive_rate_percent) },
    { icon: Layers01Icon, tone: 'blue', label: 'Fixes suggested', value: count(values.total_fixes_suggested) },
    { icon: CursorPointer01Icon, tone: 'violet', label: 'Fix acceptance rate', value: percent(values.fix_acceptance_rate_percent) },
  ];

  return (
    <article className="metrics-panel" key="metrics">
      <div className="metrics-intro"><button className="refresh-button" type="button" onClick={loadMetrics} disabled={state === 'loading'}>Refresh</button></div>
      {state === 'error' && <div className="metrics-error" role="status">Metrics unavailable.</div>}
      <div className="metrics-table-wrap">
        <table className="metrics-table">
          <thead><tr><th scope="col">MEASURE</th><th scope="col">ALL-TIME</th></tr></thead>
          <tbody>{rows.map(row => (
            <tr key={row.label}>
              <th scope="row"><span className={`metric-icon ${row.tone}`}><Icon icon={row.icon} size={17} /></span>{row.label}</th>
              <td className="metric-value">{state === 'loading' && !metrics ? '...' : row.value}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </article>
  );
}

export default function App() {
  const [activeWorkflowStep, setActiveWorkflowStep] = useState('metadata');
  const [heroBrandReady, setHeroBrandReady] = useState(false);
  const [heroTitleReady, setHeroTitleReady] = useState(false);
  const [heroAccentReady, setHeroAccentReady] = useState(false);
  const heroHeadingReady = heroTitleReady && heroAccentReady;
  const onHeroBrandComplete = useCallback(() => setHeroBrandReady(true), []);
  const onHeroTitleComplete = useCallback(() => setHeroTitleReady(true), []);
  const onHeroAccentComplete = useCallback(() => setHeroAccentReady(true), []);

  return (
    <main>
      <section className="hero" id="top">
        <div className="hero-hyperspeed" aria-hidden="true">
          <Hyperspeed effectOptions={HERO_HYPERSPEED_OPTIONS} />
        </div>
        <div className="hero-copy">
          <a className="brand" href="#top" aria-label="INNO home"><FoldHeading text="INNO" color="#f5f7f3" trigger="mount" onComplete={onHeroBrandComplete} /></a>
          <div className="hero-heading-slot">
            {heroBrandReady && <h1><FoldHeading text="PR review that" color="#f3f6f1" trigger="mount" onComplete={onHeroTitleComplete} /><br /><span><FoldHeading text="understands your code." color="#c9f36a" trigger="mount" onComplete={onHeroAccentComplete} /></span></h1>}
          </div>
          <div className={`hero-followup${heroHeadingReady ? ' is-visible' : ''}`}>
            <p className="hero-lede">INNO brings deterministic repository context, AI-powered fixes, and automatic verification into your existing CI workflow.</p>
          </div>
        </div>
      </section>

      <section className="scroll-expand-section" aria-label="INNO repository context">
        <ScrollExpand
          className="inno-scroll-expand"
          src="/inno-context-stage.svg"
          alt="A glowing map of connected repository components"
          title="Your repository, already understood."
          scrollHint="Scroll to explore"
          startWidth={58}
          startHeight={58}
          startRadius={22}
          mediaZoom={1.18}
          scrollDistance={1.05}
          holdDistance={0.28}
          smoothing={0.12}
          overlayScrim={0.5}
          useWindowScroll
        >
          <div className="scroll-stage-content">
            <div className="section-kicker">DETERMINISTIC REPOSITORY CONTEXT</div>
            <h2>Diff to exact context to verified fix.</h2>
            <p>INNO maps the repository before the AI reviews a change, so each PR gets the files and workflows that matter.</p>
          </div>
        </ScrollExpand>
      </section>

      <section className="explore-section" id="workflow">
        <div className="section-wrap">
          <div className="explore-heading workflow-heading"><div><div className="section-kicker">HOW INNO WORKS</div><h2><FoldHeading text="From commit to merge." color="#17251f" /></h2></div></div>
          <div className="explore-card workflow-card">
            <BranchedMenu
              className="explore-menu"
              items={WORKFLOW_ITEMS}
              defaultOpen={[0]}
              defaultActive="metadata"
              onSelect={setActiveWorkflowStep}
              color="#617268"
              accentColor="#8fbd57"
              lineColor="#dce5dc"
              width={250}
              rowHeight={38}
            />
            <article className="explore-content workflow-content" key={activeWorkflowStep}>
              <div className="section-kicker">{WORKFLOW_ITEMS[0].children.findIndex(item => item.value === activeWorkflowStep) + 1} / 5</div>
              <h3>{WORKFLOW_CARDS[activeWorkflowStep].title}</h3>
              <p>{WORKFLOW_CARDS[activeWorkflowStep].text}</p>
            </article>
          </div>

        </div>
      </section>
      <section className="capabilities-section" id="capabilities">
        <div className="section-wrap">
          <div className="explore-heading"><div><div className="section-kicker">02 / BUILT FOR YOUR WORKFLOW</div><h2><FoldHeading text="Useful from the" color="#f2f5f1" /><br /><span><FoldHeading text="first pull request." color="#c9f36a" /></span></h2></div><p>Bring AI-assisted review and remediation into CI while keeping your existing GitHub process and human approvals in place.</p></div>
          <ScrollStack
            className="capability-stack"
            useWindowScroll
            itemDistance={34}
            itemScale={0.025}
            itemStackDistance={18}
            stackPosition="18%"
            scaleEndPosition="8%"
            baseScale={0.86}
            rotationAmount={0.15}
            blurAmount={0.25}
          >
            <ScrollStackItem itemClassName="capability-stack-card"><article className="capability-card"><span>01</span><h3><FoldHeading text="Plug and play" /></h3><p>Add INNO to your GitHub workflow and run it in your existing CI pipeline.</p></article></ScrollStackItem>
            <ScrollStackItem itemClassName="capability-stack-card"><article className="capability-card"><span>02</span><h3><FoldHeading text="Your Copilot, your GitHub" /></h3><p>INNO uses your organization or user’s GitHub Copilot CLI. AI usage is billed directly through GitHub.</p></article></ScrollStackItem>
            <ScrollStackItem itemClassName="capability-stack-card"><article className="capability-card"><span>03</span><h3><FoldHeading text="Repository context that stays current" /></h3><p>A metadata map is incrementally updated with each commit, stored separately from source code in a dedicated branch, and avoids repeated repository exploration.</p></article></ScrollStackItem>
            <ScrollStackItem itemClassName="capability-stack-card"><article className="capability-card"><span>04</span><h3><FoldHeading text="Fixable or needs a person" /></h3><p>Each issue is clearly marked for automatic remediation or human intervention.</p></article></ScrollStackItem>
            <ScrollStackItem itemClassName="capability-stack-card"><article className="capability-card"><span>05</span><h3><FoldHeading text="Verify every fix" /></h3><p>After a fix, INNO reruns linters and scans to check whether the issue is resolved.</p></article></ScrollStackItem>
            <ScrollStackItem itemClassName="capability-stack-card"><article className="capability-card"><span>06</span><h3><FoldHeading text="PR-native, human-approved" /></h3><p>Keep findings, fixes, and validation in your existing pull request. AI proposes; your team reviews and approves before merge.</p></article></ScrollStackItem>
          </ScrollStack>
          <div className="context-comparison">
            <article><div className="section-kicker">BEFORE</div><p className="context-flow">LLM &rarr; Explore repo &rarr; Analyze &rarr; Repeat</p></article>
            <article className="context-with"><div className="section-kicker">WITH INNO</div><p className="context-flow">Metadata &rarr; Exact context &rarr; LLM &rarr; Fix &rarr; Re-scan</p></article>
          </div>
        </div>
      </section>
      <section className="metrics-section" id="metrics">
        <div className="section-wrap">
          <div className="explore-heading metrics-heading"><h2><FoldHeading text="All-time stats." color="#f2f5f1" /></h2></div>
          <MetricsPanel />
        </div>
      </section>
    </main>
  );
}
