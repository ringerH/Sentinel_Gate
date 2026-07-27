import React, { useState, useEffect } from 'react';
import { ShieldAlert, CheckCircle, XCircle, RefreshCw, Radio, Flame, Server } from 'lucide-react';

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

function App() {
  const [metrics, setMetrics] = useState({
    agent_spend_cents: 0,
    agent_limit_cents: 100000,
    fleet_spend_cents: 0,
    fleet_limit_cents: 500000,
    allowed_count: 0,
    denied_count: 0,
    escalated_count: 0,
    killswitch_fleet_active: false
  });
  
  const [approvals, setApprovals] = useState([]);
  const [logs, setLogs] = useState([]);
  const [isRefreshing, setIsRefreshing] = useState(false);

  const fetchDashboardData = async () => {
    try {
      // 1. Fetch Telemetry Metrics
      const metricsResp = await fetch(`${API_URL}/metrics`);
      if (metricsResp.ok) {
        const metricsData = await metricsResp.json();
        setMetrics(metricsData);
      }
      
      // 2. Fetch Active Escalations
      const approvalsResp = await fetch(`${API_URL}/approvals`);
      if (approvalsResp.ok) {
        const approvalsData = await approvalsResp.json();
        setApprovals(approvalsData);
      }
      
      // 3. Fetch Decision Audit Logs
      const logsResp = await fetch(`${API_URL}/logs`);
      if (logsResp.ok) {
        const logsData = await logsResp.json();
        setLogs(logsData);
      }
    } catch (err) {
      console.error("Dashboard failed to retrieve backend telemetry:", err);
    }
  };

  // Poll gateway stats every 1.5 seconds for live scrolling logs and meters
  useEffect(() => {
    fetchDashboardData();
    const interval = setInterval(fetchDashboardData, 1500);
    return () => clearInterval(interval);
  }, []);

  const handleManualRefresh = async () => {
    setIsRefreshing(true);
    await fetchDashboardData();
    setTimeout(() => setIsRefreshing(false), 500);
  };

  const handleResolveApproval = async (requestId, approved) => {
    try {
      const resp = await fetch(`${API_URL}/approvals/${requestId}/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved })
      });
      if (resp.ok) {
        fetchDashboardData();
      } else {
        const errorData = await resp.json();
        alert(`Failed to resolve approval: ${errorData.detail}`);
      }
    } catch (err) {
      console.error("Failed to connect to resolve API:", err);
    }
  };

  const handleToggleKillSwitch = async () => {
    const newState = !metrics.killswitch_fleet_active;
    try {
      const resp = await fetch(`${API_URL}/killswitch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ active: newState })
      });
      if (resp.ok) {
        fetchDashboardData();
      }
    } catch (err) {
      console.error("Failed to toggle emergency stop:", err);
    }
  };

  const formatINR = (cents) => {
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency: 'INR'
    }).format(cents / 100);
  };

  // Calculate percentage capacities
  const getPercent = (value, limit) => {
    if (!limit) return 0;
    return Math.min(100, Math.round((value / limit) * 100));
  };

  const agentSpendPercent = getPercent(metrics.agent_spend_cents, metrics.agent_limit_cents);
  const fleetSpendPercent = getPercent(metrics.fleet_spend_cents, metrics.fleet_limit_cents);

  return (
    <div className="dashboard-container">
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <h1>Governance Gateway for Financial Agents</h1>
          <div className="sub-header">OPERATOR CONTROL & REAL-TIME AUDIT TELEMETRY</div>
        </div>
        
        <div style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}>
          <button 
            className={`btn-killswitch ${metrics.killswitch_fleet_active ? 'active' : ''}`}
            onClick={handleToggleKillSwitch}
            style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}
          >
            <Flame size={16} />
            {metrics.killswitch_fleet_active ? "DEACTIVATE STOP" : "EMERGENCY STOP"}
          </button>
          
          <button 
            onClick={handleManualRefresh} 
            style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', borderColor: 'var(--border-muted)' }}
          >
            <RefreshCw size={14} className={isRefreshing ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>
      </div>

      {/* Emergency Stop Active Warning Banner */}
      {metrics.killswitch_fleet_active && (
        <div className="emergency-banner">
          <ShieldAlert size={20} style={{ display: 'inline', marginRight: '0.75rem', verticalAlign: 'middle' }} />
          FLEET-WIDE EMERGENCY STOP IS CURRENTLY ACTIVE (ALL EVALUATE REQUESTS HARD-DENIED)
        </div>
      )}

      {/* Telemetry metrics bar */}
      <div className="telemetry-grid">
        {/* Count Card */}
        <div className="card">
          <div className="card-header-bar">
            <h3>DECISION COUNTERS</h3>
            <Radio size={14} color="var(--border-gold)" />
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', padding: '0.5rem 0' }}>
            <div>
              <div style={{ fontSize: '0.8rem', color: 'var(--color-allow)', fontFamily: 'var(--font-serif-header)' }}>ALLOWED</div>
              <div className="metric-value" style={{ color: 'var(--color-allow)' }}>{metrics.allowed_count}</div>
            </div>
            <div>
              <div style={{ fontSize: '0.8rem', color: 'var(--color-deny)', fontFamily: 'var(--font-serif-header)' }}>DENIED</div>
              <div className="metric-value" style={{ color: 'var(--color-deny)' }}>{metrics.denied_count}</div>
            </div>
            <div>
              <div style={{ fontSize: '0.8rem', color: 'var(--color-escalate)', fontFamily: 'var(--font-serif-header)' }}>ESCALATED</div>
              <div className="metric-value" style={{ color: 'var(--color-escalate)' }}>{metrics.escalated_count}</div>
            </div>
          </div>
        </div>

        {/* Agent daily spend card */}
        <div className="card">
          <div className="card-header-bar">
            <h3>TRADING-AGENT BUDGET</h3>
            <Server size={14} color="var(--border-gold)" />
          </div>
          <div className="metric-value">{formatINR(metrics.agent_spend_cents)}</div>
          <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
            Daily limit: {formatINR(metrics.agent_limit_cents)}
          </div>
          <div className="progress-container">
            <div className="progress-bar-bg">
              <div 
                className={`progress-bar-fill ${agentSpendPercent > 90 ? 'danger' : agentSpendPercent > 70 ? 'warning' : ''}`}
                style={{ width: `${agentSpendPercent}%` }}
              ></div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
              <span>Capacity utilization</span>
              <span>{agentSpendPercent}%</span>
            </div>
          </div>
        </div>

        {/* Fleet daily spend card */}
        <div className="card">
          <div className="card-header-bar">
            <h3>FLEET TOTAL BUDGET</h3>
            <Server size={14} color="var(--border-gold)" />
          </div>
          <div className="metric-value">{formatINR(metrics.fleet_spend_cents)}</div>
          <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
            Daily limit: {formatINR(metrics.fleet_limit_cents)}
          </div>
          <div className="progress-container">
            <div className="progress-bar-bg">
              <div 
                className={`progress-bar-fill ${fleetSpendPercent > 90 ? 'danger' : fleetSpendPercent > 70 ? 'warning' : ''}`} 
                style={{ width: `${fleetSpendPercent}%` }}
              ></div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.75rem', color: 'var(--text-secondary)' }}>
              <span>Capacity utilization</span>
              <span>{fleetSpendPercent}%</span>
            </div>
          </div>
        </div>
      </div>

      {/* Main Split Layout */}
      <div className="dashboard-layout">
        
        {/* Left Column: Active Escalations requiring Human operator approval */}
        <div className="card" style={{ display: 'flex', flexDirection: 'column' }}>
          <div className="card-header-bar">
            <h2>ACTIVE ESCALATIONS</h2>
            <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', fontFamily: 'var(--font-serif-header)' }}>
              {approvals.length} PENDING
            </span>
          </div>
          
          <div style={{ flex: 1, overflowY: 'auto' }}>
            {approvals.length === 0 ? (
              <div style={{ textAlign: 'center', color: 'var(--text-secondary)', padding: '3rem 1rem', fontFamily: 'var(--font-serif-body)', fontStyle: 'italic' }}>
                Awaiting agent escalations...
              </div>
            ) : (
              approvals.map((appr) => {
                const expiresSecs = Math.max(0, Math.round((new Date(appr.expires_at).getTime() - new Date().getTime()) / 1000));
                
                return (
                  <div key={appr.request_id} className="escalation-item">
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontWeight: 'bold' }}>
                      <span style={{ fontFamily: 'var(--font-serif-header)' }}>{appr.agent_id}</span>
                      <span style={{ color: 'var(--color-deny)' }}>{formatINR(appr.amount)}</span>
                    </div>
                    <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginTop: '0.25rem' }}>
                      <strong>Action:</strong> {appr.action_type}
                    </div>
                    <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginTop: '0.1rem' }}>
                      <strong>Request ID:</strong> <code style={{ fontSize: '0.75rem' }}>{appr.request_id}</code>
                    </div>
                    <div style={{ fontSize: '0.85rem', color: 'var(--color-escalate)', marginTop: '0.25rem' }}>
                      <strong>Auto-Denies in:</strong> {expiresSecs}s
                    </div>
                    <div className="button-group">
                      <button 
                        className="btn-approve" 
                        onClick={() => handleResolveApproval(appr.request_id, true)}
                      >
                        APPROVE ALLOW
                      </button>
                      <button 
                        className="btn-deny" 
                        onClick={() => handleResolveApproval(appr.request_id, false)}
                      >
                        DENY BLOCK
                      </button>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* Right Column: Decision Audit Log Stream */}
        <div className="card">
          <div className="card-header-bar">
            <h2>DECISION AUDIT LEDGER</h2>
            <span style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', fontFamily: 'var(--font-serif-header)' }}>
              LIVE STREAMING
            </span>
          </div>
          
          <div className="logs-stream">
            {logs.length === 0 ? (
              <div style={{ textAlign: 'center', color: 'var(--text-secondary)', padding: '2rem 1rem' }}>
                No gateway decisions logged yet.
              </div>
            ) : (
              logs.map((log) => (
                <div key={log.request_id} className="log-entry">
                  <div className="log-meta">
                    <span>{new Date(log.timestamp).toLocaleTimeString()}</span>
                    <span>{log.agent_id}</span>
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', margin: '0.25rem 0' }}>
                    <div style={{ fontWeight: '500' }}>
                      {log.action_type.toUpperCase()} - <code>{log.request_id.substring(0, 8)}...</code>
                    </div>
                    <span className={`badge ${log.decision.toLowerCase()}`}>
                      {log.decision}
                    </span>
                  </div>
                  <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', fontStyle: 'italic' }}>
                    {log.reason}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

      </div>

      {/* Footer */}
      <div className="footer">
        EST. 2026 — DETERMINISTIC NON-AGENTIC FINOPS INFRASTRUCTURE
      </div>
    </div>
  );
}

export default App;
