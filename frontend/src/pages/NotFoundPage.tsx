import { Link } from "react-router-dom";
import { EmptyState, PageHeader } from "../components/States";

export default function NotFoundPage() {
  return (
    <>
      <PageHeader title="Not found" />
      <EmptyState
        title="That page does not exist"
        action={
          <Link className="button primary" to="/">
            Go to the dashboard
          </Link>
        }
      >
        Check the address, or start again from the dashboard.
      </EmptyState>
    </>
  );
}
