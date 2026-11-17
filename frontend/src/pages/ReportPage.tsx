import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ErrorState, PageHeader, SkeletonRows } from "../components/States";
import { downloadReportPdf, getReportHtml } from "../lib/api";

export default function ReportPage() {
  const { auditId = "" } = useParams();
  const [pdfError, setPdfError] = useState<string | null>(null);

  const report = useQuery({
    queryKey: ["report", auditId],
    queryFn: () => getReportHtml(auditId),
    enabled: Boolean(auditId),
  });

  const download = useMutation({
    mutationFn: () => downloadReportPdf(auditId),
    onError: (error) => {
      setPdfError(
        error instanceof Error
          ? error.message
          : "The PDF could not be produced by this deployment."
      );
    },
    onSuccess: () => setPdfError(null),
  });

  return (
    <>
      <PageHeader
        title="Executive report"
        subtitle="Built from stored findings, the risk assessment and review decisions only."
        actions={
          <>
            <button
              type="button"
              className="button primary"
              onClick={() => download.mutate()}
              disabled={download.isPending}
            >
              {download.isPending ? "Preparing…" : "Download PDF"}
            </button>
            <Link className="button" to={`/audits/${auditId}/findings`}>
              Findings
            </Link>
            <Link className="button" to={`/audits/${auditId}/risk`}>
              Risk
            </Link>
          </>
        }
      />

      {pdfError ? (
        <p className="form-error" role="alert">
          {pdfError}
        </p>
      ) : null}

      {report.isPending ? <SkeletonRows rows={8} label="Building the report" /> : null}
      {report.isError ? (
        <ErrorState error={report.error} onRetry={() => void report.refetch()} />
      ) : null}
      {report.isSuccess ? (
        <iframe
          className="report-frame"
          title="Executive report"
          sandbox=""
          srcDoc={report.data}
        />
      ) : null}
    </>
  );
}
