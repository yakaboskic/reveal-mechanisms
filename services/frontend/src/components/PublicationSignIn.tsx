"use client";

import { signIn } from "next-auth/react";
import { ProviderButtons, useIdentity } from "./Session";

export function PublicationSignIn({ kind, published = false }: { kind: "scientific account" | "exploration"; published?: boolean }) {
  const { ready } = useIdentity();
  return <div className="publication-signin">
    <p>{published ? `Sign in to update this published ${kind}. Changes stay private until you confirm an update.` : `Sign in to publish this ${kind}. Your work stays private until you confirm publication.`}</p>
    <ProviderButtons disabled={!ready} compact onLogin={provider => void signIn(provider, { callbackUrl: window.location.href })} />
  </div>;
}
