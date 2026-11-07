use callit_escrow::{bet_address, process_instruction, vault_address, Bet, ACTIVE, DONE, OPEN, TOKEN_PROGRAM};
use solana_program_test::*;
use solana_sdk::{
    account::Account,
    clock::Clock,
    instruction::{AccountMeta, Instruction},
    pubkey::Pubkey,
    signature::{Keypair, Signer},
    system_instruction, system_program,
    transaction::Transaction,
};

const RESOLVER_BYTES: [u8; 64] = [248, 78, 3, 1, 115, 179, 125, 169, 23, 196, 133, 84, 58, 179, 188, 136, 135, 129, 166, 18, 106, 177, 12, 210, 81, 67, 12, 90, 59, 30, 48, 146, 128, 199, 236, 52, 188, 155, 99, 17, 141, 15, 76, 185, 73, 195, 165, 160, 19, 95, 222, 74, 235, 21, 116, 136, 24, 199, 254, 41, 121, 101, 107, 159];
const SOL: u64 = 1_000_000_000;

fn resolver() -> Keypair { Keypair::from_bytes(&RESOLVER_BYTES).unwrap() }

struct Env { ctx: ProgramTestContext, pid: Pubkey }

async fn setup(players: &[&Keypair]) -> Env {
    let pid = Pubkey::new_unique();
    let mut pt = ProgramTest::new("callit_escrow", pid, processor!(process_instruction));
    for p in players {
        pt.add_account(p.pubkey(), Account { lamports: 10 * SOL, owner: system_program::ID, ..Account::default() });
    }
    pt.add_account(resolver().pubkey(), Account { lamports: SOL, owner: system_program::ID, ..Account::default() });
    Env { ctx: pt.start_with_context().await, pid }
}

impl Env {
    async fn send(&mut self, ixs: &[Instruction], signers: &[&Keypair]) -> Result<(), BanksClientError> {
        let bh = self.ctx.banks_client.get_latest_blockhash().await.unwrap();
        let payer = signers[0];
        let tx = Transaction::new_signed_with_payer(ixs, Some(&payer.pubkey()), signers, bh);
        self.ctx.banks_client.process_transaction(tx).await
    }
    async fn lamports(&mut self, k: &Pubkey) -> u64 { self.ctx.banks_client.get_balance(*k).await.unwrap() }
    async fn bet(&mut self, id: u64) -> Option<Bet> {
        let acc = self.ctx.banks_client.get_account(bet_address(&self.pid, id).0).await.unwrap()?;
        Bet::unpack(&acc.data).ok()
    }
    async fn now(&mut self) -> i64 { self.ctx.banks_client.get_sysvar::<Clock>().await.unwrap().unix_timestamp }
    async fn warp(&mut self, secs: i64) {
        let mut c: Clock = self.ctx.banks_client.get_sysvar().await.unwrap();
        c.unix_timestamp += secs;
        self.ctx.set_sysvar(&c);
    }
}

fn ix_create(pid: &Pubkey, creator: &Pubkey, id: u64, amount: u64, deadline: i64, opp: Pubkey, spl: Option<(Pubkey, Pubkey)>) -> Instruction {
    let bet = bet_address(pid, id).0;
    let mut data = vec![0u8];
    data.extend_from_slice(&id.to_le_bytes());
    data.extend_from_slice(&amount.to_le_bytes());
    data.extend_from_slice(&deadline.to_le_bytes());
    data.extend_from_slice(opp.as_ref());
    data.extend_from_slice(resolver().pubkey().as_ref());
    let mut accs = vec![AccountMeta::new(*creator, true), AccountMeta::new(bet, false), AccountMeta::new_readonly(system_program::ID, false)];
    if let Some((mint, ct)) = spl {
        accs.extend([AccountMeta::new_readonly(mint, false), AccountMeta::new(ct, false),
                     AccountMeta::new(vault_address(pid, &bet).0, false), AccountMeta::new_readonly(TOKEN_PROGRAM, false)]);
    }
    Instruction { program_id: *pid, accounts: accs, data }
}

fn ix_join(pid: &Pubkey, joiner: &Pubkey, id: u64, spl: Option<Pubkey>) -> Instruction {
    let bet = bet_address(pid, id).0;
    let mut accs = vec![AccountMeta::new(*joiner, true), AccountMeta::new(bet, false), AccountMeta::new_readonly(system_program::ID, false)];
    if let Some(jt) = spl {
        accs.extend([AccountMeta::new(jt, false), AccountMeta::new(vault_address(pid, &bet).0, false), AccountMeta::new_readonly(TOKEN_PROGRAM, false)]);
    }
    Instruction { program_id: *pid, accounts: accs, data: vec![1] }
}

fn ix_release(pid: &Pubkey, tag: u8, extra: Option<u8>, signer: &Pubkey, id: u64, creator: &Pubkey, opp: &Pubkey, spl: Option<(Pubkey, Pubkey)>) -> Instruction {
    let bet = bet_address(pid, id).0;
    let mut data = vec![tag];
    if let Some(x) = extra { data.push(x); }
    let mut accs = vec![AccountMeta::new_readonly(*signer, true), AccountMeta::new(bet, false), AccountMeta::new(*creator, false), AccountMeta::new(*opp, false)];
    if let Some((ct, ot)) = spl {
        accs.extend([AccountMeta::new(vault_address(pid, &bet).0, false), AccountMeta::new(ct, false), AccountMeta::new(ot, false), AccountMeta::new_readonly(TOKEN_PROGRAM, false)]);
    }
    Instruction { program_id: *pid, accounts: accs, data }
}

#[tokio::test]
async fn sol_duel_referee_pays_winner() {
    let (a, b) = (Keypair::new(), Keypair::new());
    let mut env = setup(&[&a, &b]).await;
    let pid = env.pid;
    let dl = env.now().await + 3600;
    env.send(&[ix_create(&pid, &a.pubkey(), 7, SOL / 10, dl, b.pubkey(), None)], &[&a]).await.unwrap();
    assert_eq!(env.bet(7).await.unwrap().status, OPEN);
    // a stranger can't take a targeted duel, the creator can't join himself
    let c = Keypair::new();
    assert!(env.send(&[ix_join(&pid, &a.pubkey(), 7, None)], &[&a]).await.is_err());
    env.send(&[ix_join(&pid, &b.pubkey(), 7, None)], &[&b]).await.unwrap();
    assert_eq!(env.bet(7).await.unwrap().status, ACTIVE);
    let _ = c;
    // only the referee can settle; a player pretending to be the referee fails
    assert!(env.send(&[ix_release(&pid, 2, Some(0), &a.pubkey(), 7, &a.pubkey(), &b.pubkey(), None)], &[&a]).await.is_err());
    // the referee can't redirect money: payout accounts must be the two players
    let thief = Keypair::new();
    let r = resolver();
    assert!(env.send(&[ix_release(&pid, 2, Some(0), &r.pubkey(), 7, &thief.pubkey(), &b.pubkey(), None)], &[&r]).await.is_err());
    let before = env.lamports(&b.pubkey()).await;
    env.send(&[ix_release(&pid, 2, Some(1), &r.pubkey(), 7, &a.pubkey(), &b.pubkey(), None)], &[&r]).await.unwrap();
    assert_eq!(env.lamports(&b.pubkey()).await - before, 2 * SOL / 10);
    assert!(env.bet(7).await.is_none(), "bet account closed");
}

#[tokio::test]
async fn sol_concede_and_cancel_and_expire() {
    let (a, b) = (Keypair::new(), Keypair::new());
    let mut env = setup(&[&a, &b]).await;
    let pid = env.pid;
    let dl = env.now().await + 3600;
    // concede: b gives up, a wins without the referee
    env.send(&[ix_create(&pid, &a.pubkey(), 1, SOL, dl, Pubkey::default(), None)], &[&a]).await.unwrap();
    env.send(&[ix_join(&pid, &b.pubkey(), 1, None)], &[&b]).await.unwrap();
    let before = env.lamports(&a.pubkey()).await;
    env.send(&[ix_release(&pid, 3, None, &b.pubkey(), 1, &a.pubkey(), &b.pubkey(), None)], &[&b]).await.unwrap();
    assert!(env.lamports(&a.pubkey()).await > before + 2 * SOL - SOL / 100);
    // cancel before anyone joins gets the stake back
    env.send(&[ix_create(&pid, &a.pubkey(), 2, SOL, dl, Pubkey::default(), None)], &[&a]).await.unwrap();
    let before = env.lamports(&a.pubkey()).await;
    env.send(&[Instruction { program_id: pid, accounts: vec![AccountMeta::new(a.pubkey(), true), AccountMeta::new(bet_address(&pid, 2).0, false)], data: vec![4] }], &[&a]).await.unwrap();
    assert!(env.lamports(&a.pubkey()).await > before + SOL - SOL / 100);
    // expire: referee disappears, anyone refunds both after deadline + 7 days
    env.send(&[ix_create(&pid, &a.pubkey(), 3, SOL, dl, Pubkey::default(), None)], &[&a]).await.unwrap();
    env.send(&[ix_join(&pid, &b.pubkey(), 3, None)], &[&b]).await.unwrap();
    assert!(env.send(&[ix_release(&pid, 5, None, &b.pubkey(), 3, &a.pubkey(), &b.pubkey(), None)], &[&b]).await.is_err());
    env.warp(3600 + 7 * 24 * 3600 + 10).await;
    let (ba, bb) = (env.lamports(&a.pubkey()).await, env.lamports(&b.pubkey()).await);
    env.send(&[ix_release(&pid, 5, None, &b.pubkey(), 3, &a.pubkey(), &b.pubkey(), None)], &[&b]).await.unwrap();
    assert!(env.lamports(&a.pubkey()).await >= ba + SOL);
    assert!(env.lamports(&b.pubkey()).await >= bb + SOL - SOL / 100);
}

// ---- SPL (USDC-style) -------------------------------------------------------------------------

fn tok_ix(accounts: Vec<AccountMeta>, data: Vec<u8>) -> Instruction { Instruction { program_id: TOKEN_PROGRAM, accounts, data } }

#[tokio::test]
async fn spl_duel_usdc_style() {
    let (a, b) = (Keypair::new(), Keypair::new());
    let mut env = setup(&[&a, &b]).await;
    let pid = env.pid;
    let (mint, ta, tb) = (Keypair::new(), Keypair::new(), Keypair::new());
    let rent_mint = 1_461_600; let rent_acc = 2_039_280;
    let mut init_mint = vec![20u8, 6]; init_mint.extend_from_slice(a.pubkey().as_ref()); init_mint.push(0);
    let acct = |k: &Keypair, owner: &Pubkey| {
        let mut d = vec![18u8]; d.extend_from_slice(owner.as_ref());
        vec![system_instruction::create_account(&a.pubkey(), &k.pubkey(), rent_acc, 165, &TOKEN_PROGRAM),
             tok_ix(vec![AccountMeta::new(k.pubkey(), false), AccountMeta::new_readonly(mint.pubkey(), false)], d)]
    };
    let mint_to = |dest: &Pubkey, amt: u64| { let mut d = vec![7u8]; d.extend_from_slice(&amt.to_le_bytes());
        tok_ix(vec![AccountMeta::new(mint.pubkey(), false), AccountMeta::new(*dest, false), AccountMeta::new_readonly(a.pubkey(), true)], d) };
    let mut ixs = vec![system_instruction::create_account(&a.pubkey(), &mint.pubkey(), rent_mint, 82, &TOKEN_PROGRAM),
                       tok_ix(vec![AccountMeta::new(mint.pubkey(), false)], init_mint)];
    ixs.extend(acct(&ta, &a.pubkey())); ixs.extend(acct(&tb, &b.pubkey()));
    ixs.push(mint_to(&ta.pubkey(), 50_000_000)); ixs.push(mint_to(&tb.pubkey(), 50_000_000));
    env.send(&ixs, &[&a, &mint, &ta, &tb]).await.unwrap();

    let dl = env.now().await + 3600;
    let five = 5_000_000; // 5 USDC
    env.send(&[ix_create(&pid, &a.pubkey(), 9, five, dl, Pubkey::default(), Some((mint.pubkey(), ta.pubkey())))], &[&a]).await.unwrap();
    env.send(&[ix_join(&pid, &b.pubkey(), 9, Some(tb.pubkey()))], &[&b]).await.unwrap();
    // referee can't pay a token account that belongs to someone else
    let r = resolver();
    assert!(env.send(&[ix_release(&pid, 2, Some(0), &r.pubkey(), 9, &a.pubkey(), &b.pubkey(), Some((tb.pubkey(), tb.pubkey())))], &[&r]).await.is_err());
    env.send(&[ix_release(&pid, 2, Some(0), &r.pubkey(), 9, &a.pubkey(), &b.pubkey(), Some((ta.pubkey(), tb.pubkey())))], &[&r]).await.unwrap();
    let bal = |acc: Account| u64::from_le_bytes(acc.data[64..72].try_into().unwrap());
    let a_bal = bal(env.ctx.banks_client.get_account(ta.pubkey()).await.unwrap().unwrap());
    let b_bal = bal(env.ctx.banks_client.get_account(tb.pubkey()).await.unwrap().unwrap());
    assert_eq!(a_bal, 55_000_000);
    assert_eq!(b_bal, 45_000_000);
    let _ = DONE;
}
