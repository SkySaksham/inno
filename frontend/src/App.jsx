import { useEffect, useState } from 'react';
import {
  CursorPointer01Icon,
  Layers01Icon,
  Notification03Icon,
  Settings02Icon,
} from '@hugeicons/core-free-icons';
import { HugeiconsIcon } from '@hugeicons/react';
import BranchedMenu from './components/BranchedMenu.jsx';

// Set this to the deployed metrics-backend /metrics/summary endpoint.
const METRICS_API_URL = 'http://127.0.0.1:5000/metrics/summary';

const sections = {
  static: {
    eyebrow: '01 / STATIC SIGNALS',
    title: 'Known risks, caught early.',
    description: 'Pylint and Bandit scan changed Python code for established quality and security problems. Inno reviews each signal with nearby source and diff context before it becomes a finding.',
    tags: ['Pylint', 'Bandit', 'Changed-line focus'],
  },
  semantic: {
    eyebrow: '02 / SEMANTIC CONTEXT',
    title: 'Review the change in context.',
    description: 'A separate AI pass looks for issues linters cannot recognize: behavior changes, weakened checks, and mismatches between a function and its callers.',
    tags: ['Function-level diff', 'Caller context', 'Evidence required'],
  },
  suggestions: {
    eyebrow: '03 / SUGGESTED FIXES',
    title: 'A fix you can apply in one click.',
    description: 'When a replacement is grounded in current code and maps safely to changed lines, Inno posts a native GitHub suggestion. Reviewers stay in control of applying it.',
    tags: ['Minimal replacement', 'Diff-anchored', 'Human-approved'],
  },
};

function Icon({ icon, size = 18 }) {
  return <HugeiconsIcon icon={icon} size={size} strokeWidth={1.8} />;
}

function FeaturePanel({ value }) {
  const item = sections[value];
  if (!item) return null;
  return (
    <article className="explore-panel" key={value}>
      <div className="panel-topline"><span>{item.eyebrow}</span><span className="panel-status"><i /> ACTIVE REVIEW LAYER</span></div>
      <h3>{item.title}</h3>
      <p>{item.description}</p>
      <div className="panel-tags">{item.tags.map(tag => <span key={tag}>{tag}</span>)}</div>
      <div className="panel-diagram">
        <div className="diagram-node"><Icon icon={value === 'static' ? Settings02Icon : value === 'semantic' ? Layers01Icon : CursorPointer01Icon} size={19} /><span>{value === 'static' ? 'Analyzer signal' : value === 'semantic' ? 'Changed behavior' : 'Safe replacement'}</span></div>
        <span className="diagram-connector"><i /></span>
        <div className="diagram-node diagram-result"><Icon icon={value === 'suggestions' ? CursorPointer01Icon : Notification03Icon} size={19} /><span>{value === 'suggestions' ? 'GitHub suggestion' : 'Evidence-backed finding'}</span></div>
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
      <div className="metrics-intro"><div><h3>Honesty, measured over time.</h3><p>All-time review outcomes, not a promise of perfection.</p></div><button className="refresh-button" type="button" onClick={loadMetrics} disabled={state === 'loading'}><span>↻</span> Refresh</button></div>
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
        <div className="hero-copy">
          <a className="brand" href="#top" aria-label="Inno home"><span className="brand-mark">in</span><span>inno<span className="brand-period">.</span></span></a>
          <div className="eyebrow"><span className="pulse" /> PULL REQUEST INTELLIGENCE</div>
          <h1>Catch the subtle.<br /><span>Fix with confidence.</span></h1>
          <p className="hero-lede">Inno pairs proven static analysis with AI that reads each change in context—so reviews surface real risks and give maintainers a practical next step.</p>
          <a className="button primary" href="#explore">Explore how it works <span>↓</span></a>
          <div className="hero-proof"><span><i /> STATIC ANALYSIS</span><b>+</b><span><i /> SEMANTIC REVIEW</span><b>+</b><span>HUMAN JUDGMENT</span></div>
        </div>
        <div className="hero-visual" aria-label="Illustration of a code review finding and suggested fix">
          <div className="visual-orbit orbit-one" /><div className="visual-orbit orbit-two" />
          <div className="visual-core">in<i /></div>
          <div className="code-card">
            <div className="code-head"><span className="dots"><i /><i /><i /></span><span>auth.py</span><span className="code-state">REVIEWED</span></div>
            <div className="code-row muted"><b>41</b><code>if user.is_admin:</code></div>
            <div className="code-row alert"><b>42</b><code>    return get_data()</code><span className="alert-pin">!</span></div>
            <div className="code-note"><span className="note-icon">↳</span><span><strong>Authorization check missing</strong><small>High confidence · changed line</small></span></div>
            <div className="suggestion"><span className="suggestion-label">SUGGESTED FIX</span><span className="suggestion-code">if user.is_admin and user.is_active:</span><span className="apply-pill">＋ Apply suggestion</span></div>
          </div>
          <div className="float-tag tag-static">◈ STATIC SIGNAL</div><div className="float-tag tag-context">✳ CHANGE CONTEXT</div>
        </div>
      </section>

      <section className="explore-section" id="explore">
        <div className="section-wrap">
          <div className="explore-heading"><div><div className="section-kicker">01 / TAKE A CLOSER LOOK</div><h2>One review.<br /><span>Multiple perspectives.</span></h2></div><p>Choose a layer to see what makes Inno different. Review signals are grounded in code, and proposed fixes stay subject to human approval.</p></div>
          <div className="explore-card">
            <BranchedMenu defaultActive={activeSection} onSelect={setActiveSection} className="explore-menu" />
            <div className="explore-content" aria-live="polite">
              {activeSection === 'metrics' ? <MetricsPanel /> : <FeaturePanel value={activeSection} />}
            </div>
          </div>
          <div className="explore-foot"><span>INNO / PR REVIEW SYSTEM</span><a href="https://github.com/SkySaksham/inno" target="_blank" rel="noreferrer">Explore the source <span aria-hidden="true">→</span></a></div>
        </div>
      </section>
      <footer className="footer"><span>inno<span className="brand-period">.</span></span><span>Reviews with evidence. Fixes with intent.</span><span>OPEN SOURCE · BUILT FOR PULL REQUESTS</span></footer>
    </main>
  );
}
