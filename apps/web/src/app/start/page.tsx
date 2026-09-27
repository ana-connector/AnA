import { redirect } from "next/navigation";

// Older bookmarks still reach the new root start page.
export default function OldStartPage() {
  redirect("/");
}
