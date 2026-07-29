import { redirect } from "next/navigation";

// The sidebar is the nav now -- no separate landing screen needed.
export default function Home() {
  redirect("/dashboard");
}
