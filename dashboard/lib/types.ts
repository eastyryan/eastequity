// Shapes of the JSON the agent publishes into dashboard/data/. Shared by the
// Arena front page and the /ledger deep-dive so the two can't drift apart.

export type Position = {
  ticker: string;
  quantity: number;
  avg_cost: number;
  market_value_usd: number;
  last_price?: number;
  unrealized_pct?: number | null;
  notional_usd?: number | null;
  opened_at: string;
  days_held?: number;
  sector?: string | null;
  original_plan?: {
    thesis?: string;
    stop_loss?: number;
    target_price?: number;
    entry_price_max?: number;
    holding_horizon_days?: number;
    confidence?: number;
    risk_reward_ratio?: number;
    catalysts?: string[];
    risk_map?: string;
    macro_context?: string;
    proposed_at?: string;
  } | null;
};

export type Indicator = { latest: number; direction: string } | null;

export type CalibrationBucket = {
  trades: number;
  win_rate_pct: number;
  avg_stated_confidence_pct: number;
  calibration_gap_pct: number;
};

export type Calibration = {
  note: string;
  by_confidence: Record<string, CalibrationBucket>;
  high_conf_0_70_plus: { trades: number; win_rate_pct: number | null; inflated: boolean };
};

export type PositionRisk = {
  last_price: number;
  recorded_stop: number;
  cushion_to_stop_pct: number | null;
  atr_pct: number | null;
  expected_move_pct: number | null;
  cushion_in_atr?: number;
  inside_noise_band?: boolean;
  stop_distance_from_entry_pct?: number;
};

export type ClosedTrade = {
  ticker: string;
  entry_price: number;
  exit_price: number;
  opened_at: string;
  closed_at: string;
  days_held: number;
  pnl_usd: number | null;
  r_multiple: number | null;
  verdict: string;
  thesis?: string | null;
  confidence?: number | null;
  dividends_usd?: number;
  total_pnl_usd?: number;
  fees_usd?: { commission?: number; sec_fee?: number; taf?: number } | null;
  gap_modeled?: boolean;
};

// action stays a strict union rather than widening to string: EquityChart keys
// its BUY/SELL markers off it, and `| string` would collapse the union and
// silently disable that exhaustiveness.
export type TradeEvent = {
  date: string;
  ticker: string;
  action: "BUY" | "SELL_TO_CLOSE";
  price?: number | null;
  verdict?: string | null;
};

export type WatchlistItem = {
  ticker: string;
  one_line: string;
  thoughts: string;
  would_buy_at?: string | null;
  status?: string;
};

export type WatchlistOutcome = {
  ticker: string;
  first_watched: string;
  price_when_added?: number | null;
  would_buy_at?: string | null;
  one_line?: string;
  currently_watched?: boolean;
  hit_buy_level?: boolean;
  hit_date?: string;
  acted?: boolean;
  latest_price?: number | null;
  move_pct_since_watched?: number | null;
  dropped_date?: string;
};

export type PositionChartData = {
  bars: { date: string; open: number; high: number; low: number; close: number }[];
  avg_cost?: number | null;
  last_price?: number | null;
  entry?: number | null;
  stop?: number | null;
  target?: number | null;
  opened_at?: string | null;
};

export type UniverseLogEntry = {
  date: string;
  added: string[];
  removed: string[];
  dropped_unpriceable: string[];
  size: number;
  rationale: string;
};

export type HistoryPoint = {
  date: string;
  equity: number;
  cash?: number;
  benchmark_close?: number | null;
};

export type Latest = {
  generated_at: string;
  run_id: string;
  mode: string;
  schedule_note?: string;
  portfolio: { cash_usd: number; total_equity_usd: number; positions: Position[] };
  no_trade_reason?: string | null;
  commentary?: string | null;
  macro_snapshot?: {
    cpi_yoy_pct: Indicator;
    ten_year_yield: Indicator;
    vix: Indicator;
    yield_curve_10y2y?: Indicator;
    hy_credit_spread?: Indicator;
  } | null;
  macro_regime_hint?: {
    score_0_to_5?: number;
    label?: string;
    series_missing?: string[] | null;
    note?: string;
  } | null;
  proposals: { proposal: Record<string, unknown>; approved: boolean; reasons: string[] }[];
  fills: { ticker: string; action: string; fill_price: number; quantity: number }[];
  closed_trades?: ClosedTrade[];
  performance?: {
    closed_trades: number;
    win_rate_pct: number;
    realized_pnl_usd: number;
    avg_r_multiple: number | null;
    avg_days_held: number;
    max_drawdown_pct: number;
    realized_pnl_incl_dividends_usd?: number;
    total_fees_paid_usd?: number;
  } | null;
  improvements?: { date: string; note: string }[];
  watchlist?: WatchlistItem[];
  total_dividends_usd?: number;
  calibration?: Calibration | null;
  position_risk?: Record<string, PositionRisk>;
  data_quality?: { source: string; age_hours?: number; stale?: boolean; note?: string } | null;
  stale_data_notice?: string | null;
  universe_size?: number;
  as_of_et?: string;
  trade_events?: TradeEvent[];
  watchlist_outcomes?: WatchlistOutcome[];
  health?: {
    as_of_et?: string;
    expected_runs_so_far?: number;
    completed_scheduled_runs?: number;
    missed?: number;
    /** ET labels of the slots that were missed, e.g. ["14:00"]. */
    missed_slots?: string[];
    /** Which execution node(s) actually ran today — a dead node is visible here. */
    nodes_seen?: string[];
    bundle_age_hours?: number | null;
    status?: string;
  } | null;
  risk_halts?: string[];
  forced_exits?: {
    ticker: string;
    reason: string;
    last_price?: number | null;
    stop_loss?: number | null;
    days_held?: number | null;
    horizon?: number | null;
    note?: string;
  }[];
};
