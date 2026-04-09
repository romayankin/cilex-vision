/**
 * JWT role-checking utilities for client-side RBAC.
 *
 * The JWT is stored in an httpOnly cookie named "access_token".
 * Since httpOnly cookies are not readable from JS, we parse a
 * non-httpOnly "user_role" hint cookie that the auth service sets
 * alongside the JWT.  If neither cookie exists (dev mode), all
 * pages are accessible.
 */

export type UserRole = "admin" | "operator" | "viewer" | "engineering";

/**
 * Get the current user role from cookie or return null (dev/unauthenticated).
 */
export function getUserRole(): UserRole | null {
  if (typeof document === "undefined") return null;

  const match = document.cookie
    .split("; ")
    .find((c) => c.startsWith("user_role="));
  if (!match) return null;

  const role = match.split("=")[1] as UserRole;
  if (["admin", "operator", "viewer", "engineering"].includes(role)) {
    return role;
  }
  return null;
}

/** viewer, operator, admin can access events */
export function canAccessEvents(role: UserRole | null): boolean {
  if (role === null) return true; // dev mode
  return ["viewer", "operator", "admin"].includes(role);
}

/** engineering and admin can access debug traces */
export function canAccessDebug(role: UserRole | null): boolean {
  if (role === null) return true;
  return ["engineering", "admin"].includes(role);
}

/** admin only */
export function isAdmin(role: UserRole | null): boolean {
  if (role === null) return true; // dev mode shows everything
  return role === "admin";
}
