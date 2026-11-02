"""ATM Simulator - Minor Project (Python, Tkinter, SQLite)

Run:  python atm_simulator.py
Demo cards (created on first run):
    1111222233334444  PIN 2222  (Demo User)
    5555666677778888  PIN 4321   (Second User)
Admin password: admin123
"""
import sqlite3
import hashlib
import random
import os
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from datetime import datetime

DB = "atm.db"
ADMIN_PASSWORD = "admin123"
DAILY_LIMIT = 20000
MAX_ATTEMPTS = 3
MIN_BALANCE = 500
BANK_NAME = "SMART BANK"  # change this to rename the bank (logo letter updates too)
SESSION_TIMEOUT_MS = 60000  # auto-logout after 60 s of inactivity


# ----------------------------- DATABASE LAYER -----------------------------
def conn():
    return sqlite3.connect(DB)


def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS accounts(
            account_no INTEGER PRIMARY KEY AUTOINCREMENT,
            card_no TEXT UNIQUE NOT NULL, holder_name TEXT NOT NULL,
            pin_hash TEXT NOT NULL, balance REAL DEFAULT 0,
            status TEXT DEFAULT 'active', failed_attempts INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS transactions(
            txn_id INTEGER PRIMARY KEY AUTOINCREMENT, account_no INTEGER,
            type TEXT, amount REAL, balance_after REAL, timestamp TEXT,
            remarks TEXT, FOREIGN KEY(account_no) REFERENCES accounts(account_no))""")
        c.execute("""CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, event TEXT, detail TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS atm_cash(denom INTEGER PRIMARY KEY, notes INTEGER)")
        if c.execute("SELECT COUNT(*) FROM atm_cash").fetchone()[0] == 0:
            c.executemany("INSERT INTO atm_cash VALUES(?,?)", [(500, 50), (200, 50), (100, 100)])
        if c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0:
            for card, name, pin, bal in [("1111222233334444", "Demo User", "1234", 50000),
                                         ("5555666677778888", "Second User", "4321", 25000)]:
                c.execute("INSERT INTO accounts(card_no,holder_name,pin_hash,balance) "
                          "VALUES(?,?,?,?)", (card, name, hash_pin(pin), bal))


def log(c, acc, typ, amount, bal, remarks=""):
    c.execute("INSERT INTO transactions(account_no,type,amount,balance_after,timestamp,remarks) "
              "VALUES(?,?,?,?,?,?)", (acc, typ, amount, bal, now(), remarks))


def write_receipt(c, acc, typ, amount, bal):
    name, card = c.execute("SELECT holder_name,card_no FROM accounts WHERE account_no=?",
                           (acc,)).fetchone()
    os.makedirs("receipts", exist_ok=True)
    path = os.path.join("receipts", f"receipt_{datetime.now():%Y%m%d_%H%M%S}_{acc}.txt")
    with open(path, "w") as f:
        f.write(f"===== {BANK_NAME} ATM =====\n"
                f"Date/Time : {now()}\nCustomer  : {name}\n"
                f"Card No   : XXXX-XXXX-XXXX-{card[-4:]}\n"
                f"Type      : {typ.upper()}\nAmount    : Rs.{amount:.2f}\n"
                f"Balance   : Rs.{bal:.2f}\n==================================\n")
    return path


def login(card, pin):
    with conn() as c:
        row = c.execute("SELECT account_no,pin_hash,status,failed_attempts FROM accounts "
                        "WHERE card_no=?", (card,)).fetchone()
        if not row:
            return None, "Card not found"
        acc, pin_hash, status, fails = row
        if status == "blocked":
            return None, "Card is blocked. Contact admin."
        if hash_pin(pin) == pin_hash:
            c.execute("UPDATE accounts SET failed_attempts=0 WHERE account_no=?", (acc,))
            audit(c, "LOGIN_OK", f"account {acc}")
            return acc, "OK"
        fails += 1
        blocked = fails >= MAX_ATTEMPTS
        audit(c, "CARD_BLOCKED" if blocked else "LOGIN_FAIL", f"account {acc}")
        c.execute("UPDATE accounts SET failed_attempts=?, status=? WHERE account_no=?",
                  (fails, "blocked" if blocked else "active", acc))
        return None, ("Card blocked after 3 wrong attempts." if blocked
                      else f"Wrong PIN. {MAX_ATTEMPTS - fails} attempt(s) left.")


def get_account(acc):
    with conn() as c:
        return c.execute("SELECT holder_name,balance,card_no FROM accounts WHERE account_no=?",
                         (acc,)).fetchone()


def withdraw(acc, amount):
    if amount <= 0 or amount % 100 != 0:
        return False, "Amount must be a positive multiple of 100."
    with conn() as c:
        bal = c.execute("SELECT balance FROM accounts WHERE account_no=?", (acc,)).fetchone()[0]
        today = datetime.now().strftime("%Y-%m-%d") + "%"
        used = c.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE account_no=? "
                         "AND type='withdraw' AND timestamp LIKE ?", (acc, today)).fetchone()[0]
        if amount > bal:
            return False, "Insufficient balance."
        if bal - amount < MIN_BALANCE:
            return False, f"Minimum balance of Rs.{MIN_BALANCE} must be maintained."
        if used + amount > DAILY_LIMIT:
            return False, f"Daily limit of Rs.{DAILY_LIMIT} exceeded."
        plan = plan_notes(c, amount)
        if not plan:
            return False, "ATM cannot dispense this amount (low cash). Try another amount."
        for d, n in plan.items():
            c.execute("UPDATE atm_cash SET notes=notes-? WHERE denom=?", (n, d))
        new = bal - amount
        c.execute("UPDATE accounts SET balance=? WHERE account_no=?", (new, acc))
        log(c, acc, "withdraw", amount, new)
        write_receipt(c, acc, "withdraw", amount, new)
        return True, (f"Please collect Rs.{amount:.0f}: "
                      + ", ".join(f"{n} x Rs.{d}" for d, n in plan.items())
                      + f"\nBalance: Rs.{new:.2f}\nReceipt saved in /receipts.")


def deposit(acc, amount):
    if amount <= 0:
        return False, "Enter a valid amount."
    with conn() as c:
        bal = c.execute("SELECT balance FROM accounts WHERE account_no=?", (acc,)).fetchone()[0]
        new = bal + amount
        c.execute("UPDATE accounts SET balance=? WHERE account_no=?", (new, acc))
        log(c, acc, "deposit", amount, new)
        write_receipt(c, acc, "deposit", amount, new)
        return True, f"Rs.{amount:.0f} deposited.\nBalance: Rs.{new:.2f}"


def transfer(acc, to_card, amount):
    if amount <= 0:
        return False, "Enter a valid amount."
    with conn() as c:
        dest = c.execute("SELECT account_no,status FROM accounts WHERE card_no=?",
                         (to_card,)).fetchone()
        if not dest or dest[1] != "active":
            return False, "Receiver account not found or blocked."
        if dest[0] == acc:
            return False, "Cannot transfer to your own account."
        bal = c.execute("SELECT balance FROM accounts WHERE account_no=?", (acc,)).fetchone()[0]
        if amount > bal:
            return False, "Insufficient balance."
        c.execute("UPDATE accounts SET balance=balance-? WHERE account_no=?", (amount, acc))
        c.execute("UPDATE accounts SET balance=balance+? WHERE account_no=?", (amount, dest[0]))
        nb = bal - amount
        rb = c.execute("SELECT balance FROM accounts WHERE account_no=?", (dest[0],)).fetchone()[0]
        log(c, acc, "transfer_out", amount, nb, f"to {to_card}")
        log(c, dest[0], "transfer_in", amount, rb, "from account " + str(acc))
        write_receipt(c, acc, "transfer", amount, nb)
        return True, f"Rs.{amount:.0f} transferred.\nBalance: Rs.{nb:.2f}"


def change_pin(acc, old, new):
    if not (new.isdigit() and len(new) == 4):
        return False, "New PIN must be exactly 4 digits."
    with conn() as c:
        cur = c.execute("SELECT pin_hash FROM accounts WHERE account_no=?", (acc,)).fetchone()[0]
        if cur != hash_pin(old):
            return False, "Old PIN is incorrect."
        c.execute("UPDATE accounts SET pin_hash=? WHERE account_no=?", (hash_pin(new), acc))
        log(c, acc, "pin_change", 0, 0)
        return True, "PIN changed successfully."


def statement(acc, n=10):
    with conn() as c:
        return c.execute("SELECT timestamp,type,amount,balance_after FROM transactions "
                         "WHERE account_no=? ORDER BY txn_id DESC LIMIT ?", (acc, n)).fetchall()


def plan_notes(c, amount):
    """Greedy note dispensing using the ATM's current cash stock."""
    stock = dict(c.execute("SELECT denom,notes FROM atm_cash"))
    plan, left = {}, amount
    for d in sorted(stock, reverse=True):
        n = min(left // d, stock[d])
        if n:
            plan[d], left = n, left - n * d
    return plan if left == 0 else None


def audit(c, event, detail=""):
    c.execute("INSERT INTO audit_log(timestamp,event,detail) VALUES(?,?,?)",
              (now(), event, detail))


def audit_rows():
    with conn() as c:
        return c.execute("SELECT timestamp,event,detail FROM audit_log "
                         "ORDER BY id DESC LIMIT 200").fetchall()


def summary():
    with conn() as c:
        rows = c.execute("SELECT type,COUNT(*),COALESCE(SUM(amount),0) FROM transactions "
                         "GROUP BY type").fetchall()
        cash = c.execute("SELECT denom,notes FROM atm_cash ORDER BY denom DESC").fetchall()
        return rows, cash


def export_csv(path="transactions_export.csv"):
    import csv
    with conn() as c, open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "account_no", "type", "amount", "balance_after"])
        w.writerows(c.execute("SELECT timestamp,account_no,type,amount,balance_after "
                              "FROM transactions ORDER BY txn_id"))
    return path


# admin helpers
def create_account(name, pin, opening):
    card = "".join(random.choice("0123456789") for _ in range(16))
    with conn() as c:
        c.execute("INSERT INTO accounts(card_no,holder_name,pin_hash,balance) VALUES(?,?,?,?)",
                  (card, name, hash_pin(pin), opening))
    return card


def all_accounts():
    with conn() as c:
        return c.execute("SELECT account_no,card_no,holder_name,balance,status "
                         "FROM accounts").fetchall()


def set_status(acc, status):
    with conn() as c:
        c.execute("UPDATE accounts SET status=?, failed_attempts=0 WHERE account_no=?",
                  (status, acc))
        audit(c, "ADMIN_" + status.upper(), f"account {acc}")


def all_transactions():
    with conn() as c:
        return c.execute("SELECT timestamp,account_no,type,amount,balance_after FROM transactions "
                         "ORDER BY txn_id DESC LIMIT 200").fetchall()


# --------------------------------- GUI ---------------------------------
class ATMApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{BANK_NAME} ATM")
        self.geometry("460x590")
        self.configure(bg="#0b3d6b")
        self.resizable(False, False)
        icon = tk.PhotoImage(width=32, height=32)
        for x in range(32):
            for y in range(32):
                d = (x - 15.5) ** 2 + (y - 15.5) ** 2
                if d <= 240:
                    icon.put("#ffd34d" if d > 150 else "#0b3d6b", (x, y))
        self._icon = icon
        self.iconphoto(True, icon)
        self.acc = None
        self._timer = None
        self.bind_all("<Any-KeyPress>", self.reset_timer)
        self.bind_all("<Button-1>", self.reset_timer)
        self.login_screen()

    def reset_timer(self, *_):
        if self._timer:
            self.after_cancel(self._timer)
        self._timer = self.after(SESSION_TIMEOUT_MS, self.timeout) if self.acc else None

    def timeout(self):
        messagebox.showinfo("Session expired", "Logged out due to inactivity.")
        self.login_screen()

    def clear(self):
        for w in self.winfo_children():
            w.destroy()

    def btn(self, parent, text, cmd, color="#1f7acb"):
        return tk.Button(parent, text=text, command=cmd, bg=color, fg="white",
                         font=("Arial", 12, "bold"), relief="flat", width=24, pady=6,
                         activebackground="#155a96", activeforeground="white", cursor="hand2")

    def header(self, text):
        logo = tk.Canvas(self, width=70, height=70, bg="#0b3d6b", highlightthickness=0)
        logo.pack(pady=(15, 0))
        logo.create_oval(3, 3, 67, 67, fill="#ffd34d", outline="")
        logo.create_oval(10, 10, 60, 60, fill="#0b3d6b", outline="")
        logo.create_text(35, 35, text=BANK_NAME[0], fill="#ffd34d", font=("Georgia", 26, "bold"))
        tk.Label(self, text=BANK_NAME, bg="#0b3d6b", fg="#ffd34d",
                 font=("Georgia", 18, "bold")).pack(pady=(4, 0))
        tk.Label(self, text=text, bg="#0b3d6b", fg="white", font=("Arial", 12)).pack(pady=5)

    # ---- login ----
    def login_screen(self):
        self.clear()
        self.acc = None
        self.reset_timer()
        self.header("Please insert your card and enter PIN")
        box = tk.Frame(self, bg="#0b3d6b")
        box.pack(pady=25)
        tk.Label(box, text="Card Number", bg="#0b3d6b", fg="white").pack()
        self.card_e = tk.Entry(box, font=("Arial", 14), width=22, justify="center")
        self.card_e.pack(pady=5)
        tk.Label(box, text="PIN", bg="#0b3d6b", fg="white").pack()
        self.pin_e = tk.Entry(box, font=("Arial", 14), width=22, show="*", justify="center")
        self.pin_e.pack(pady=5)
        self.pin_e.bind("<Return>", lambda e: self.do_login())
        self.btn(self, "LOGIN", self.do_login).pack(pady=10)
        self.btn(self, "ADMIN PANEL", self.admin_login, "#6b6b6b").pack(pady=5)

    def do_login(self):
        acc, msg = login(self.card_e.get().strip(), self.pin_e.get().strip())
        if acc:
            self.acc = acc
            self.menu_screen()
        else:
            messagebox.showerror("Login failed", msg)
            self.pin_e.delete(0, tk.END)

    # ---- user menu ----
    def menu_screen(self):
        self.clear()
        name = get_account(self.acc)[0]
        self.header(f"Welcome, {name}")
        self.reset_timer()
        for text, cmd in [("Balance Enquiry", self.show_balance),
                          ("Withdraw Cash", self.do_withdraw),
                          ("Deposit Cash", self.do_deposit),
                          ("Fund Transfer", self.do_transfer),
                          ("Mini Statement", self.show_statement),
                          ("Change PIN", self.do_change_pin)]:
            self.btn(self, text, cmd).pack(pady=5)
        self.btn(self, "LOGOUT / EJECT CARD", self.login_screen, "#c0392b").pack(pady=15)

    def show_balance(self):
        messagebox.showinfo("Balance", f"Available balance: Rs.{get_account(self.acc)[1]:.2f}")

    def ask_amount(self, title):
        return simpledialog.askinteger(title, "Enter amount (Rs.):", parent=self, minvalue=1)

    def do_withdraw(self):
        amt = self.ask_amount("Withdraw")
        if amt:
            ok, msg = withdraw(self.acc, amt)
            (messagebox.showinfo if ok else messagebox.showerror)("Withdraw", msg)

    def do_deposit(self):
        amt = self.ask_amount("Deposit")
        if amt:
            ok, msg = deposit(self.acc, amt)
            (messagebox.showinfo if ok else messagebox.showerror)("Deposit", msg)

    def do_transfer(self):
        card = simpledialog.askstring("Transfer", "Receiver card number:", parent=self)
        if not card:
            return
        amt = self.ask_amount("Transfer")
        if amt:
            ok, msg = transfer(self.acc, card.strip(), amt)
            (messagebox.showinfo if ok else messagebox.showerror)("Transfer", msg)

    def do_change_pin(self):
        old = simpledialog.askstring("Change PIN", "Old PIN:", show="*", parent=self)
        new = simpledialog.askstring("Change PIN", "New 4-digit PIN:", show="*", parent=self)
        if old and new:
            ok, msg = change_pin(self.acc, old, new)
            (messagebox.showinfo if ok else messagebox.showerror)("Change PIN", msg)

    def table_window(self, title, cols, rows):
        win = tk.Toplevel(self)
        win.title(title)
        tree = ttk.Treeview(win, columns=cols, show="headings", height=14)
        for col in cols:
            tree.heading(col, text=col)
            tree.column(col, width=130, anchor="center")
        for r in rows:
            tree.insert("", tk.END, values=r)
        tree.pack(fill="both", expand=True, padx=10, pady=10)

    def show_statement(self):
        self.table_window("Mini Statement", ("Date/Time", "Type", "Amount", "Balance"),
                          statement(self.acc))

    # ---- admin ----
    def admin_login(self):
        pw = simpledialog.askstring("Admin", "Enter admin password:", show="*", parent=self)
        if pw == ADMIN_PASSWORD:
            self.admin_screen()
        elif pw is not None:
            messagebox.showerror("Admin", "Wrong password.")

    def admin_screen(self):
        self.clear()
        self.geometry("860x520")
        tk.Label(self, text="ADMIN PANEL", bg="#0b3d6b", fg="#ffd34d",
                 font=("Arial", 18, "bold")).pack(pady=10)
        cols = ("Acc No", "Card No", "Name", "Balance", "Status")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=12)
        for col in cols:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=140, anchor="center")
        self.tree.pack(padx=10, pady=5)
        self.refresh_accounts()
        bar = tk.Frame(self, bg="#0b3d6b")
        bar.pack(pady=10)
        for text, cmd in [("Create Account", self.admin_create),
                          ("Block", lambda: self.admin_set("blocked")),
                          ("Unblock", lambda: self.admin_set("active")),
                          ("All Transactions", self.admin_txns),
                          ("Dashboard", self.admin_dashboard),
                          ("Audit Log", self.admin_audit),
                          ("Export CSV", self.admin_export)]:
            tk.Button(bar, text=text, command=cmd, width=12, pady=4).pack(side="left", padx=4)
        self.btn(self, "BACK TO ATM", self.back_to_atm, "#c0392b").pack(pady=10)

    def back_to_atm(self):
        self.geometry("460x590")
        self.login_screen()

    def refresh_accounts(self):
        self.tree.delete(*self.tree.get_children())
        for r in all_accounts():
            self.tree.insert("", tk.END, values=r)

    def admin_create(self):
        name = simpledialog.askstring("New account", "Holder name:", parent=self)
        pin = simpledialog.askstring("New account", "4-digit PIN:", show="*", parent=self)
        opening = simpledialog.askinteger("New account", "Opening balance:", parent=self,
                                          minvalue=0)
        if name and pin and pin.isdigit() and len(pin) == 4 and opening is not None:
            card = create_account(name, pin, opening)
            self.refresh_accounts()
            messagebox.showinfo("Created", f"Account created.\nCard number: {card}")
        else:
            messagebox.showerror("Error", "Invalid details (PIN must be 4 digits).")

    def admin_set(self, status):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("Select", "Select an account first.")
            return
        set_status(self.tree.item(sel[0])["values"][0], status)
        self.refresh_accounts()

    def admin_txns(self):
        self.table_window("All Transactions",
                          ("Date/Time", "Acc No", "Type", "Amount", "Balance"),
                          all_transactions())

    def admin_audit(self):
        self.table_window("Audit Log", ("Date/Time", "Event", "Detail"), audit_rows())

    def admin_export(self):
        messagebox.showinfo("Export", f"Saved to {export_csv()}")

    def admin_dashboard(self):
        rows, cash = summary()
        win = tk.Toplevel(self)
        win.title("Dashboard - total amount by transaction type")
        cv = tk.Canvas(win, width=480, height=290, bg="white")
        cv.pack(padx=10, pady=10)
        top = max([r[2] for r in rows] + [1])
        for i, (typ, cnt, tot) in enumerate(rows):
            x, h = 40 + i * 75, 190 * tot / top
            cv.create_rectangle(x, 240 - h, x + 50, 240, fill="#1f7acb")
            cv.create_text(x + 25, 252, text=typ, font=("Arial", 8))
            cv.create_text(x + 25, 230 - h, text=f"{tot:.0f} ({cnt})", font=("Arial", 8))
        tk.Label(win, text="ATM cash left: " + ", ".join(f"{n} x Rs.{d}" for d, n in cash)).pack(pady=5)


if __name__ == "__main__":
    init_db()
    ATMApp().mainloop()
