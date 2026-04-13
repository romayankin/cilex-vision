import { Suspense } from "react";

export default function LiveLayout({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<div>Loading cameras...</div>}>{children}</Suspense>;
}
