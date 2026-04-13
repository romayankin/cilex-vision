import { Suspense } from "react";

export default function StorageLayout({ children }: { children: React.ReactNode }) {
  return (
    <Suspense fallback={<div className="text-sm text-gray-400">Loading…</div>}>
      {children}
    </Suspense>
  );
}
