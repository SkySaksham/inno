import React, { useEffect, useState } from 'react';
import {
  CursorPointer01Icon,
  Layers01Icon,
  Notification03Icon,
  Settings02Icon,
} from '@hugeicons/core-free-icons';
import { HugeiconsIcon } from '@hugeicons/react';
import BranchedMenu from './components/BranchedMenu.jsx';
import Hyperspeed from './components/Hyperspeed.jsx';
import FoldText from './components/FoldText.jsx';

// Set this to the deployed metrics-backend /metrics/summary endpoint.
const METRICS_API_URL = 'http://127.0.0.1:5000/metrics/summary';
const HERO_HYPERSPEED_OPTIONS = {
  colors: {
    roadColor: 0x111713,
    islandColor: 0x171e19,
    background: 0x101511,
    shoulderLines: 0x49624e,
    brokenLines: 0x637f69,
    leftCars: [0x18382c, 0x548039, 0x71af46],
    rightCars: [0x6d974f, 0x8caf72, 0x324555],
    sticks: 0x83bb47,
  },
};

const sections = {
  static: {
    eyebrow: '01 / LINT → SCAN → UNDERSTAND',
    title: 'Every pull request gets a complete review.',
    description: 'INNO runs linters plus semantic and security scans on each PR. It identifies the exact changes and the repository components or workflows they affect.',
    tags: ['Runs in your CI', 'Changed code focus', 'Security and quality'],
  },
  semantic: {
    eyebrow: '02 / DETERMINISTIC CONTEXT',
    title: 'Give the AI the context it needs.',
    description: 'A continuously updated repository metadata map identifies relevant files, components, and workflows. The AI can focus on the change without repeatedly re-exploring the repository.',
    tags: ['Repository map', 'Exact relevant context', 'Updated with every commit'],
  },
  suggestions: {
    eyebrow: '03 / AI-POWERED REMEDIATION',
    title: 'Propose a fix, then verify it.',
    description: 'INNO explains each issue, marks whether it can be remediated automatically, and suggests a precise fix. Linters and scans rerun after the change; your team reviews and approves before anything is merged.',
    tags: ['Actionable fix', 'Automatic re-scan', 'Human approved'],
  },
};

function Icon({ icon, size = 18 }) {
  return <HugeiconsIcon icon={icon} size={size} strokeWidth={1.8} />;
}

function FoldHeading({ text, color = 'inherit', trigger = 'scroll' }) {
  return (
    <FoldText
      text={text}
      splitBy="word"
      hinge="top"
      trigger={trigger}
      duration={0.65}
      stagger={0.045}
      ease="power3.out"
      perspective={700}
      creaseShading={0.4}
      fontSize="inherit"
      fontWeight="inherit"
      color={color}
    />
  );
}

function FeaturePanel({ value }) {
  const item = sections[value];
  if (!item) return null;
  return (
    <article className="explore-panel" key={value}>
      <div className="panel-topline"><span>{item.eyebrow}</span><span className="panel-status"><i /> ACTIVE REVIEW LAYER</span></div>
      <h3><FoldHeading text={item.title} color="#223b2e" /></h3>
      <p>{item.description}</p>
      <div className="panel-tags">{item.tags.map(tag => <span key={tag}>{tag}</span>)}</div>
      <div className="panel-diagram">
        <div className="diagram-node"><Icon icon={value === 'static' ? Settings02Icon : value === 'semantic' ? Layers01Icon : CursorPointer01Icon} size={19} /><span>{value === 'static' ? 'PR checks' : value === 'semantic' ? 'Exact context' : 'Proposed fix'}</span></div>
        <span className="diagram-connector"><i /></span>
        <div className="diagram-node diagram-result"><Icon icon={value === 'suggestions' ? CursorPointer01Icon : Notification03Icon} size={19} /><span>{value === 'suggestions' ? 'Re-scan + review' : 'Evidence-backed result'}</span></div>
        <span className="diagram-arrow" aria-hidden="true">→</span>
      </div>
    </article>
  );
}

function MetricsPanel() {
  const [metrics, setMetrics] = useState(null);
  const [state, setState] = useState('loading');
  const [updatedAt, setUpdatedAt] = useState(null);

  async function loadMetrics() {
    setState('loading');
    try {
      const response = await fetch(METRICS_API_URL, { headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error(`Metrics API returned ${response.status}`);
      const payload = await response.json();
      setMetrics(payload);
      setUpdatedAt(new Date());
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
    { icon: Settings02Icon, tone: 'mint', label: 'Findings reviewed', value: count(values.total_findings), note: 'Findings assessed across all reviews' },
    { icon: Notification03Icon, tone: 'amber', label: 'False-positive rate', value: percent(values.false_positive_rate_percent), note: `${count(values.total_false_positives)} findings marked as false positives` },
    { icon: Layers01Icon, tone: 'blue', label: 'Fixes suggested', value: count(values.total_fixes_suggested), note: 'Code fixes proposed across all reviews' },
    { icon: CursorPointer01Icon, tone: 'violet', label: 'Fix acceptance rate', value: percent(values.fix_acceptance_rate_percent), note: `${count(values.total_fixes_accepted)} suggestions applied by maintainers` },
  ];

  return (
    <article className="metrics-panel" key="metrics">
      <div className="panel-topline"><span>04 / REVIEW RELIABILITY</span><span className={`panel-status ${state}`}><i /> {state === 'success' ? 'LIVE AGGREGATE' : state === 'error' ? 'API UNAVAILABLE' : 'FETCHING METRICS'}</span></div>
      <div className="metrics-intro"><div><h3><FoldHeading text="Honesty, measured over time." color="#f0f4ef" /></h3><p>All-time review outcomes, not a promise of perfection.</p></div><button className="refresh-button" type="button" onClick={loadMetrics} disabled={state === 'loading'}><span>↻</span> Refresh</button></div>
      {state === 'error' && <div className="metrics-error" role="status">Could not reach the metrics API. Check its URL and allowed frontend origin.</div>}
      <div className="metrics-table-wrap">
        <table className="metrics-table">
          <thead><tr><th scope="col">MEASURE</th><th scope="col">ALL-TIME</th><th scope="col">DETAIL</th></tr></thead>
          <tbody>{rows.map(row => (
            <tr key={row.label}>
              <th scope="row"><span className={`metric-icon ${row.tone}`}><Icon icon={row.icon} size={17} /></span>{row.label}</th>
              <td className="metric-value">{state === 'loading' && !metrics ? '—' : row.value}</td>
              <td className="metric-note">{row.note}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <div className="metrics-foot"><span><i /> SUPABASE AGGREGATE</span><span>{updatedAt ? `Updated ${updatedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : 'Updated —'}</span></div>
      <p className="metrics-method">False-positive rate = false positives ÷ findings reviewed. Fix acceptance = accepted suggestions ÷ fixes suggested.</p>
    </article>
  );
}

export default function App() {
  const [activeSection, setActiveSection] = useState('static');

  return (
    <main>
      <section className="hero" id="top">
        <div className="hero-hyperspeed" aria-hidden="true">
          <Hyperspeed effectOptions={HERO_HYPERSPEED_OPTIONS} />
        </div>
        <div className="hero-copy">
          <a className="brand" href="#top" aria-label="Inno home"><span className="brand-mark">in</span><span>inno<span className="brand-period">.</span></span></a>
          <div className="eyebrow"><span className="pulse" /> CONTEXT-AWARE AI CODE REVIEW</div>
          <h1><FoldHeading text="PR review that" color="#f3f6f1" trigger="mount" /><br /><span><FoldHeading text="understands your code." color="#c9f36a" trigger="mount" /></span></h1>
          <p className="hero-lede">INNO brings deterministic repository context, AI-powered fixes, and automatic verification into your existing CI workflow.</p>
          <a className="button primary" href="#workflow">See how INNO works <span>→</span></a>
          <div className="hero-proof"><span><i /> LINT</span><b>→</b><span><i /> UNDERSTAND</span><b>→</b><span>FIX &amp; VERIFY</span></div>
        </div>
      </section>

      <section className="explore-section" id="workflow">
        <div className="section-wrap">
          <div className="explore-heading"><div><div className="section-kicker">01 / HOW INNO WORKS</div><h2><FoldHeading text="From PR diff" color="#17251f" /><br /><span><FoldHeading text="to verified fix." color="#668b4d" /></span></h2></div><p>INNO fits into the GitHub workflow your team already uses. Explore how it scans changes, builds context, and helps your team remediate issues safely.</p></div>
          <div className="explore-card">
            <BranchedMenu defaultActive={activeSection} onSelect={setActiveSection} className="explore-menu" />
            <div className="explore-content" aria-live="polite">
              <FeaturePanel value={activeSection} />
            </div>
          </div>
          <div className="explore-foot"><span>INNO / PR REVIEW SYSTEM</span><a href="https://github.com/SkySaksham/inno" target="_blank" rel="noreferrer">Explore the source <span aria-hidden="true">→</span></a></div>
        </div>
      </section>
      <section className="capabilities-section" id="capabilities">
        <div className="section-wrap">
          <div className="explore-heading"><div><div className="section-kicker">02 / BUILT FOR YOUR WORKFLOW</div><h2><FoldHeading text="Useful from the" color="#f2f5f1" /><br /><span><FoldHeading text="first pull request." color="#c9f36a" /></span></h2></div><p>Bring AI-assisted review and remediation into CI while keeping your existing GitHub process and human approvals in place.</p></div>
          <div className="capability-grid">
            <article className="capability-card"><span>01</span><h3><FoldHeading text="Plug and play" /></h3><p>Add INNO to your GitHub workflow and run it in your existing CI pipeline.</p></article>
            <article className="capability-card"><span>02</span><h3><FoldHeading text="Your Copilot, your GitHub" /></h3><p>INNO uses your organization or user’s GitHub Copilot CLI. AI usage is billed directly through GitHub.</p></article>
            <article className="capability-card"><span>03</span><h3><FoldHeading text="Repository context that stays current" /></h3><p>A metadata map is incrementally updated with each commit, stored separately from source code in a dedicated branch, and avoids repeated repository exploration.</p></article>
            <article className="capability-card"><span>04</span><h3><FoldHeading text="Fixable or needs a person" /></h3><p>Each issue is clearly marked for automatic remediation or human intervention.</p></article>
            <article className="capability-card"><span>05</span><h3><FoldHeading text="Verify every fix" /></h3><p>After a fix, INNO reruns linters and scans to check whether the issue is resolved.</p></article>
            <article className="capability-card"><span>06</span><h3><FoldHeading text="PR-native, human-approved" /></h3><p>Keep findings, fixes, and validation in your existing pull request. AI proposes; your team reviews and approves before merge.</p></article>
          </div>
          <div className="context-comparison">
            <article><div className="section-kicker">BEFORE INNO</div><h3><FoldHeading text="Repeated discovery" /></h3><p className="context-flow">LLM <b>→</b> Explore repository <b>→</b> Discover context <b>→</b> Analyze <b>→</b> Repeat</p><p className="context-note">Redundant exploration. More tokens. More latency. Less deterministic context.</p></article>
            <article className="context-with"><div className="section-kicker">WITH INNO</div><h3><FoldHeading text="A context-aware remediation loop" /></h3><p className="context-flow">Diff <b>→</b> Metadata <b>→</b> Exact context <b>→</b> LLM <b>→</b> Fix <b>→</b> Re-scan</p><p className="context-note">The tooling understands the repository first. The AI gets exactly the context it needs.</p></article>
          </div>
          <p className="closing-line">INNO turns your CI pipeline into a context-aware AI remediation loop.</p>
        </div>
      </section>
      <section className="metrics-section" id="metrics">
        <div className="section-wrap">
          <div className="explore-heading"><div><div className="section-kicker">03 / REVIEW OUTCOMES</div><h2><FoldHeading text="All-time stats." color="#f2f5f1" /><br /><span><FoldHeading text="Clear and actionable." color="#c9f36a" /></span></h2></div><p>Track findings reviewed, false-positive rate, fixes suggested, and maintainer acceptance across all reviews.</p></div>
          <MetricsPanel />
        </div>
      </section>
    </main>
  );
}
