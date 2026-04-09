"use client";

import "./globals.css";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { getUserRole, isAdmin, canAccessDebug } from "@/lib/auth";

const NAV_ITEMS = [
  { href: "/search", label: "Search" },
  { href: "/timeline", label: "Timeline" },
  { href: "/journey", label: "Journey" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const role = getUserRole();

  return (
    <html lang="en">
      <body className="min-h-screen bg-gray-50 text-gray-900">
        <nav className="bg-white border-b border-gray-200 px-4 py-3">
          <div className="max-w-7xl mx-auto flex items-center justify-between">
            <Link href="/search" className="text-lg font-semibold text-gray-900">
              Cilex Vision
            </Link>
            <div className="flex items-center gap-1">
              {NAV_ITEMS.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`px-3 py-1.5 rounded text-sm font-medium transition-colors ${
                    pathname?.startsWith(item.href)
                      ? "bg-blue-100 text-blue-700"
                      : "text-gray-600 hover:text-gray-900 hover:bg-gray-100"
                  }`}
                >
                  {item.label}
                </Link>
              ))}
              {isAdmin(role) && (
                <Link
                  href="/admin"
                  className={`px-3 py-1.5 rounded text-sm font-medium transition-colors ${
                    pathname === "/admin"
                      ? "bg-blue-100 text-blue-700"
                      : "text-gray-600 hover:text-gray-900 hover:bg-gray-100"
                  }`}
                >
                  Admin
                </Link>
              )}
              {canAccessDebug(role) && (
                <span className="ml-2 px-2 py-0.5 text-xs bg-yellow-100 text-yellow-800 rounded">
                  {role ?? "dev"}
                </span>
              )}
            </div>
          </div>
        </nav>
        <main className="max-w-7xl mx-auto px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
