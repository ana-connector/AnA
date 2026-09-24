/** Toasts, in the three shapes this console actually needs.
 *
 *  The Base UI toast manager takes `add({ title, description, type })`; every
 *  page calling that by hand would spell the option names slightly
 *  differently, so the vocabulary lives here instead. */
import { toast } from "@/components/ui/toast";

export const notify = {
  success(title: string, description?: string) {
    toast.add({ title, description, type: "success" });
  },
  error(title: string, description?: string) {
    toast.add({ title, description, type: "error" });
  },
  info(title: string, description?: string) {
    toast.add({ title, description, type: "info" });
  },
};
