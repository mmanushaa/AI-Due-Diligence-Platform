import type { FactorRow } from "../lib/api";
import { SeverityBadge } from "./Badges";

/** "Show the math": every factor behind a score, in the order it was multiplied. */
export default function ScoreBreakdown({ factors }: { factors: FactorRow[] }) {
  if (factors.length === 0) {
    return <p className="muted">No per-finding factors were stored for this assessment.</p>;
  }
  return (
    <div className="table-wrap">
      <table>
        <caption className="visually-hidden">Risk factors per finding</caption>
        <thead>
          <tr>
            <th scope="col">Finding</th>
            <th scope="col">Severity</th>
            <th scope="col" className="align-right">
              Weight
            </th>
            <th scope="col" className="align-right">
              Confidence
            </th>
            <th scope="col" className="align-right">
              Exposure
            </th>
            <th scope="col" className="align-right">
              Score
            </th>
          </tr>
        </thead>
        <tbody>
          {factors.map((factor) => (
            <tr key={factor.finding_id}>
              <td>
                <details>
                  <summary>{factor.title}</summary>
                  <p className="mono muted">{factor.notes ?? "factors not recorded"}</p>
                  {factor.hints.length ? (
                    <p className="muted">Exposure hints: {factor.hints.join(", ")}</p>
                  ) : null}
                  <p className="mono muted">{factor.finding_id}</p>
                </details>
              </td>
              <td>
                <SeverityBadge value={factor.severity} />
              </td>
              <td className="align-right mono">{factor.severity_weight}</td>
              <td className="align-right mono">{factor.evidence_confidence.toFixed(2)}</td>
              <td className="align-right mono">{factor.exposure_multiplier.toFixed(2)}</td>
              <td className="align-right mono">{factor.finding_score.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
