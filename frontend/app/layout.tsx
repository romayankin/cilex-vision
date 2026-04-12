"use client";

import "./globals.css";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { getUserRole, isAdmin, canAccessDebug } from "@/lib/auth";

const NAV_ITEMS = [
  { href: "/search", label: "Search" },
  { href: "/timeline", label: "Timeline" },
  { href: "/journey", label: "Journey" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const role = getUserRole();

  const isLoginPage = pathname === "/login";

  async function handleLogout() {
    await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
    document.cookie = "user_role=; path=/; max-age=0";
    router.push("/login");
  }

  return (
    <html lang="en">
      <body className="min-h-screen bg-gray-50 text-gray-900">
        <nav className="bg-white border-b border-gray-200 px-4 py-3">
          <div className="max-w-7xl mx-auto flex items-center justify-between">
            <Link href="/search" className="text-lg font-semibold text-gray-900">
              Cilex Vision
            </Link>
            {!isLoginPage && (
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
                    href="/portal"
                    className={`px-3 py-1.5 rounded text-sm font-medium transition-colors ${
                      pathname?.startsWith("/portal")
                        ? "bg-blue-100 text-blue-700"
                        : "text-gray-600 hover:text-gray-900 hover:bg-gray-100"
                    }`}
                  >
                    Portal
                  </Link>
                )}
                {isAdmin(role) && (
                  <Link
                    href="/admin"
                    className={`px-3 py-1.5 rounded text-sm font-medium transition-colors ${
                      pathname?.startsWith("/admin")
                        ? "bg-blue-100 text-blue-700"
                        : "text-gray-600 hover:text-gray-900 hover:bg-gray-100"
                    }`}
                  >
                    Admin
                  </Link>
                )}
                {role ? (
                  <>
                    <span className="ml-2 px-2 py-0.5 text-xs bg-yellow-100 text-yellow-800 rounded">
                      {role}
                    </span>
                    <button
                      onClick={handleLogout}
                      className="ml-1 px-2 py-0.5 text-xs text-gray-500 hover:text-gray-700"
                    >
                      Logout
                    </button>
                  </>
                ) : (
                  <Link
                    href="/login"
                    className="ml-2 px-3 py-1.5 text-sm font-medium text-blue-600 hover:text-blue-800"
                  >
                    Login
                  </Link>
                )}
              </div>
            )}
          </div>
        </nav>
        <main className="max-w-7xl mx-auto px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
