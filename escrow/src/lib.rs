//! CALLIT duel escrow.
//!
//! Two players lock equal stakes (SOL or one SPL token such as USDC/USDT) in a bet account
//! owned by this program. Money can only ever leave to one of those two players:
//!   * `Settle`  - the referee named in the bet (the CALLIT bot) picks the winner, or refunds
//!   * `Concede` - either player gives the win to the other, no referee needed
//!   * `Cancel`  - the creator takes their stake back before anyone joins
//!   * `Expire`  - if the referee never settles, anyone can refund both players 7 days after the deadline
//! There is no admin withdraw, no fee and no way to pay a third party.

use solana_program::{
    account_info::{next_account_info, AccountInfo},
    clock::Clock,
    entrypoint::ProgramResult,
    msg,
    program::{invoke, invoke_signed},
    program_error::ProgramError,
    pubkey::Pubkey,
    rent::Rent,
    system_instruction, system_program,
    sysvar::Sysvar,
};

#[cfg(not(feature = "no-entrypoint"))]
solana_program::entrypoint!(process_instruction);

/// Classic SPL Token program (USDC and USDT on Solana use it).
pub const TOKEN_PROGRAM: Pubkey = solana_program::pubkey!("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");

pub const BET_LEN: usize = 160;
pub const TOKEN_ACCOUNT_LEN: u64 = 165;
pub const EXPIRE_GRACE: i64 = 7 * 24 * 3600;

pub const OPEN: u8 = 0;
pub const ACTIVE: u8 = 1;
pub const DONE: u8 = 2;

pub const KIND_SOL: u8 = 0;
pub const KIND_SPL: u8 = 1;

#[derive(Debug, Clone, PartialEq)]
pub struct Bet {
    pub status: u8,
    pub kind: u8,
    pub bump: u8,
    pub vault_bump: u8,
    pub bet_id: u64,
    pub amount: u64,
    pub deadline: i64,
    pub creator: Pubkey,
    pub opponent: Pubkey,
    pub mint: Pubkey,
    pub winner: u8,
    /// The referee allowed to settle this bet. The joining player sees and accepts it.
    pub resolver: Pubkey,
}

impl Bet {
    pub fn unpack(d: &[u8]) -> Result<Self, ProgramError> {
        if d.len() < 157 {
            return Err(ProgramError::InvalidAccountData);
        }
        let pk = |a: usize| Pubkey::new_from_array(d[a..a + 32].try_into().unwrap());
        let u = |a: usize| u64::from_le_bytes(d[a..a + 8].try_into().unwrap());
        Ok(Bet {
            status: d[0],
            kind: d[1],
            bump: d[2],
            vault_bump: d[3],
            bet_id: u(4),
            amount: u(12),
            deadline: u(20) as i64,
            creator: pk(28),
            opponent: pk(60),
            mint: pk(92),
            winner: d[124],
            resolver: pk(125),
        })
    }

    pub fn pack(&self, d: &mut [u8]) {
        d[0] = self.status;
        d[1] = self.kind;
        d[2] = self.bump;
        d[3] = self.vault_bump;
        d[4..12].copy_from_slice(&self.bet_id.to_le_bytes());
        d[12..20].copy_from_slice(&self.amount.to_le_bytes());
        d[20..28].copy_from_slice(&self.deadline.to_le_bytes());
        d[28..60].copy_from_slice(self.creator.as_ref());
        d[60..92].copy_from_slice(self.opponent.as_ref());
        d[92..124].copy_from_slice(self.mint.as_ref());
        d[124] = self.winner;
        d[125..157].copy_from_slice(self.resolver.as_ref());
    }
}

pub fn bet_address(program_id: &Pubkey, bet_id: u64) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[b"bet", &bet_id.to_le_bytes()], program_id)
}

pub fn vault_address(program_id: &Pubkey, bet: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(&[b"vault", bet.as_ref()], program_id)
}

fn err(m: &str) -> ProgramError {
    msg!("callit: {}", m);
    ProgramError::Custom(1)
}

fn read_u64(d: &[u8], at: usize) -> Result<u64, ProgramError> {
    d.get(at..at + 8)
        .map(|b| u64::from_le_bytes(b.try_into().unwrap()))
        .ok_or(ProgramError::InvalidInstructionData)
}

// --- minimal SPL Token instructions (avoids a heavy dependency) ---------------------------

fn token_ix(accounts: Vec<solana_program::instruction::AccountMeta>, data: Vec<u8>) -> solana_program::instruction::Instruction {
    solana_program::instruction::Instruction { program_id: TOKEN_PROGRAM, accounts, data }
}

fn ix_transfer(from: &Pubkey, to: &Pubkey, authority: &Pubkey, amount: u64) -> solana_program::instruction::Instruction {
    use solana_program::instruction::AccountMeta;
    let mut data = vec![3u8];
    data.extend_from_slice(&amount.to_le_bytes());
    token_ix(vec![AccountMeta::new(*from, false), AccountMeta::new(*to, false), AccountMeta::new_readonly(*authority, true)], data)
}

fn ix_init_account3(account: &Pubkey, mint: &Pubkey, owner: &Pubkey) -> solana_program::instruction::Instruction {
    use solana_program::instruction::AccountMeta;
    let mut data = vec![18u8];
    data.extend_from_slice(owner.as_ref());
    token_ix(vec![AccountMeta::new(*account, false), AccountMeta::new_readonly(*mint, false)], data)
}

fn ix_close(account: &Pubkey, dest: &Pubkey, authority: &Pubkey) -> solana_program::instruction::Instruction {
    use solana_program::instruction::AccountMeta;
    token_ix(vec![AccountMeta::new(*account, false), AccountMeta::new(*dest, false), AccountMeta::new_readonly(*authority, true)], vec![9u8])
}

/// Token account owner check: (mint, owner) live at bytes 0..32 and 32..64.
fn check_token_account(acc: &AccountInfo, mint: &Pubkey, owner: &Pubkey) -> ProgramResult {
    if acc.owner != &TOKEN_PROGRAM {
        return Err(err("not a token account"));
    }
    let d = acc.try_borrow_data()?;
    if d.len() < 64 || &d[0..32] != mint.as_ref() || &d[32..64] != owner.as_ref() {
        return Err(err("token account does not belong to that player"));
    }
    Ok(())
}

// --- entry -----------------------------------------------------------------------------------

pub fn process_instruction(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    let (&tag, rest) = data.split_first().ok_or(ProgramError::InvalidInstructionData)?;
    match tag {
        0 => create(program_id, accounts, rest),
        1 => join(program_id, accounts),
        2 => {
            let w = *rest.first().ok_or(ProgramError::InvalidInstructionData)?;
            release(program_id, accounts, Release::Settle(w))
        }
        3 => release(program_id, accounts, Release::Concede),
        4 => cancel(program_id, accounts),
        5 => release(program_id, accounts, Release::Expire),
        _ => Err(ProgramError::InvalidInstructionData),
    }
}

fn create(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    let it = &mut accounts.iter();
    let creator = next_account_info(it)?;
    let bet_ai = next_account_info(it)?;
    let system = next_account_info(it)?;
    if !creator.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    if system.key != &system_program::ID {
        return Err(ProgramError::IncorrectProgramId);
    }
    let bet_id = read_u64(data, 0)?;
    let amount = read_u64(data, 8)?;
    let deadline = read_u64(data, 16)? as i64;
    let opponent = Pubkey::new_from_array(data.get(24..56).ok_or(ProgramError::InvalidInstructionData)?.try_into().unwrap());
    let resolver = Pubkey::new_from_array(data.get(56..88).ok_or(ProgramError::InvalidInstructionData)?.try_into().unwrap());
    if resolver == *creator.key || resolver == Pubkey::default() {
        return Err(err("the referee can't be a player"));
    }
    if amount == 0 {
        return Err(err("stake must be more than zero"));
    }
    if deadline <= Clock::get()?.unix_timestamp {
        return Err(err("deadline already passed"));
    }
    if opponent == *creator.key {
        return Err(err("you can't duel yourself"));
    }
    let (expected, bump) = bet_address(program_id, bet_id);
    if bet_ai.key != &expected {
        return Err(err("wrong bet address"));
    }
    if bet_ai.lamports() > 0 {
        return Err(err("bet id already used"));
    }
    let rent = Rent::get()?;
    let spl = it.len() > 0;
    let seeds: &[&[u8]] = &[b"bet", &bet_id.to_le_bytes(), &[bump]];
    let mut bet = Bet {
        status: OPEN, kind: KIND_SOL, bump, vault_bump: 0, bet_id, amount, deadline,
        creator: *creator.key, opponent, mint: Pubkey::default(), winner: 0, resolver,
    };
    if !spl {
        let lamports = rent.minimum_balance(BET_LEN).checked_add(amount).ok_or(ProgramError::ArithmeticOverflow)?;
        invoke_signed(
            &system_instruction::create_account(creator.key, bet_ai.key, lamports, BET_LEN as u64, program_id),
            &[creator.clone(), bet_ai.clone(), system.clone()],
            &[seeds],
        )?;
    } else {
        let mint = next_account_info(it)?;
        let creator_token = next_account_info(it)?;
        let vault = next_account_info(it)?;
        let token = next_account_info(it)?;
        if token.key != &TOKEN_PROGRAM || mint.owner != &TOKEN_PROGRAM {
            return Err(err("only classic SPL tokens are supported"));
        }
        check_token_account(creator_token, mint.key, creator.key)?;
        let (vault_key, vault_bump) = vault_address(program_id, bet_ai.key);
        if vault.key != &vault_key {
            return Err(err("wrong vault address"));
        }
        invoke_signed(
            &system_instruction::create_account(creator.key, bet_ai.key, rent.minimum_balance(BET_LEN), BET_LEN as u64, program_id),
            &[creator.clone(), bet_ai.clone(), system.clone()],
            &[seeds],
        )?;
        let vseeds: &[&[u8]] = &[b"vault", bet_ai.key.as_ref(), &[vault_bump]];
        invoke_signed(
            &system_instruction::create_account(creator.key, vault.key, rent.minimum_balance(TOKEN_ACCOUNT_LEN as usize), TOKEN_ACCOUNT_LEN, &TOKEN_PROGRAM),
            &[creator.clone(), vault.clone(), system.clone()],
            &[vseeds],
        )?;
        invoke(&ix_init_account3(vault.key, mint.key, bet_ai.key), &[vault.clone(), mint.clone(), token.clone()])?;
        invoke(&ix_transfer(creator_token.key, vault.key, creator.key, amount), &[creator_token.clone(), vault.clone(), creator.clone(), token.clone()])?;
        bet.kind = KIND_SPL;
        bet.vault_bump = vault_bump;
        bet.mint = *mint.key;
    }
    bet.pack(&mut bet_ai.try_borrow_mut_data()?);
    msg!("callit: bet {} created, stake {}", bet_id, amount);
    Ok(())
}

fn load(program_id: &Pubkey, bet_ai: &AccountInfo) -> Result<Bet, ProgramError> {
    if bet_ai.owner != program_id {
        return Err(err("not a CALLIT bet"));
    }
    let bet = Bet::unpack(&bet_ai.try_borrow_data()?)?;
    if bet_address(program_id, bet.bet_id).0 != *bet_ai.key {
        return Err(err("bet address mismatch"));
    }
    Ok(bet)
}

fn join(program_id: &Pubkey, accounts: &[AccountInfo]) -> ProgramResult {
    let it = &mut accounts.iter();
    let joiner = next_account_info(it)?;
    let bet_ai = next_account_info(it)?;
    let system = next_account_info(it)?;
    if !joiner.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    let mut bet = load(program_id, bet_ai)?;
    if bet.status != OPEN {
        return Err(err("this duel is no longer open"));
    }
    if Clock::get()?.unix_timestamp >= bet.deadline {
        return Err(err("too late to join"));
    }
    if joiner.key == &bet.creator || joiner.key == &bet.resolver {
        return Err(err("you can't join this duel"));
    }
    if bet.opponent != Pubkey::default() && joiner.key != &bet.opponent {
        return Err(err("this duel is for someone else"));
    }
    if bet.kind == KIND_SOL {
        if system.key != &system_program::ID {
            return Err(ProgramError::IncorrectProgramId);
        }
        invoke(
            &system_instruction::transfer(joiner.key, bet_ai.key, bet.amount),
            &[joiner.clone(), bet_ai.clone(), system.clone()],
        )?;
    } else {
        let joiner_token = next_account_info(it)?;
        let vault = next_account_info(it)?;
        let token = next_account_info(it)?;
        if token.key != &TOKEN_PROGRAM || vault.key != &vault_address(program_id, bet_ai.key).0 {
            return Err(err("wrong vault"));
        }
        check_token_account(joiner_token, &bet.mint, joiner.key)?;
        invoke(&ix_transfer(joiner_token.key, vault.key, joiner.key, bet.amount), &[joiner_token.clone(), vault.clone(), joiner.clone(), token.clone()])?;
    }
    bet.opponent = *joiner.key;
    bet.status = ACTIVE;
    bet.pack(&mut bet_ai.try_borrow_mut_data()?);
    msg!("callit: bet {} is live", bet.bet_id);
    Ok(())
}

enum Release {
    Settle(u8),
    Concede,
    Expire,
}

/// Pays out (or refunds) and closes the bet. Destinations are always the stored players.
fn release(program_id: &Pubkey, accounts: &[AccountInfo], how: Release) -> ProgramResult {
    let it = &mut accounts.iter();
    let signer = next_account_info(it)?;
    let bet_ai = next_account_info(it)?;
    let creator = next_account_info(it)?;
    let opponent = next_account_info(it)?;
    if !signer.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    let mut bet = load(program_id, bet_ai)?;
    if bet.status == DONE {
        return Err(err("already settled"));
    }
    if creator.key != &bet.creator || (bet.status == ACTIVE && opponent.key != &bet.opponent) {
        return Err(err("payout accounts must be the two players"));
    }
    let now = Clock::get()?.unix_timestamp;
    // outcome: 0 = creator wins, 1 = opponent wins, 2 = refund
    let outcome = match how {
        Release::Settle(w) => {
            if signer.key != &bet.resolver {
                return Err(err("only the CALLIT referee can settle"));
            }
            if w > 2 || (bet.status == OPEN && w != 2) {
                return Err(err("invalid result"));
            }
            w
        }
        Release::Concede => {
            if bet.status != ACTIVE {
                return Err(err("nothing to concede yet"));
            }
            if signer.key == &bet.creator {
                1
            } else if signer.key == &bet.opponent {
                0
            } else {
                return Err(err("only a player can concede"));
            }
        }
        Release::Expire => {
            let grace = if bet.status == OPEN { 0 } else { EXPIRE_GRACE };
            if now <= bet.deadline.saturating_add(grace) {
                return Err(err("not expired yet"));
            }
            2
        }
    };
    let pot = if bet.status == ACTIVE { bet.amount * 2 } else { bet.amount };
    let (to_creator, to_opponent) = match (outcome, bet.status) {
        (0, _) => (pot, 0),
        (1, _) => (0, pot),
        (_, ACTIVE) => (bet.amount, bet.amount),
        _ => (bet.amount, 0),
    };

    if bet.kind == KIND_SOL {
        move_lamports(bet_ai, creator, to_creator)?;
        if to_opponent > 0 {
            move_lamports(bet_ai, opponent, to_opponent)?;
        }
    } else {
        let vault = next_account_info(it)?;
        let creator_token = next_account_info(it)?;
        let opponent_token = next_account_info(it)?;
        let token = next_account_info(it)?;
        if token.key != &TOKEN_PROGRAM || vault.key != &vault_address(program_id, bet_ai.key).0 {
            return Err(err("wrong vault"));
        }
        let seeds: &[&[u8]] = &[b"bet", &bet.bet_id.to_le_bytes(), &[bet.bump]];
        if to_creator > 0 {
            check_token_account(creator_token, &bet.mint, &bet.creator)?;
            invoke_signed(&ix_transfer(vault.key, creator_token.key, bet_ai.key, to_creator),
                          &[vault.clone(), creator_token.clone(), bet_ai.clone(), token.clone()], &[seeds])?;
        }
        if to_opponent > 0 {
            check_token_account(opponent_token, &bet.mint, &bet.opponent)?;
            invoke_signed(&ix_transfer(vault.key, opponent_token.key, bet_ai.key, to_opponent),
                          &[vault.clone(), opponent_token.clone(), bet_ai.clone(), token.clone()], &[seeds])?;
        }
        // vault rent goes back to the creator who paid it
        invoke_signed(&ix_close(vault.key, creator.key, bet_ai.key),
                      &[vault.clone(), creator.clone(), bet_ai.clone(), token.clone()], &[seeds])?;
    }

    bet.status = DONE;
    bet.winner = outcome;
    bet.pack(&mut bet_ai.try_borrow_mut_data()?);
    // close the bet account: remaining rent back to the creator
    let rest = bet_ai.lamports();
    move_lamports(bet_ai, creator, rest)?;
    bet_ai.try_borrow_mut_data()?.fill(0);
    msg!("callit: bet {} released, outcome {}", bet.bet_id, outcome);
    Ok(())
}

fn cancel(program_id: &Pubkey, accounts: &[AccountInfo]) -> ProgramResult {
    let it = &mut accounts.iter();
    let creator = next_account_info(it)?;
    let bet_ai = next_account_info(it)?;
    let bet = load(program_id, bet_ai)?;
    if !creator.is_signer || creator.key != &bet.creator {
        return Err(err("only the creator can cancel"));
    }
    if bet.status != OPEN {
        return Err(err("someone already joined, it can't be cancelled"));
    }
    if bet.kind == KIND_SOL {
        let all = bet_ai.lamports();
        move_lamports(bet_ai, creator, all)?;
    } else {
        let vault = next_account_info(it)?;
        let creator_token = next_account_info(it)?;
        let token = next_account_info(it)?;
        if token.key != &TOKEN_PROGRAM || vault.key != &vault_address(program_id, bet_ai.key).0 {
            return Err(err("wrong vault"));
        }
        check_token_account(creator_token, &bet.mint, &bet.creator)?;
        let seeds: &[&[u8]] = &[b"bet", &bet.bet_id.to_le_bytes(), &[bet.bump]];
        invoke_signed(&ix_transfer(vault.key, creator_token.key, bet_ai.key, bet.amount),
                      &[vault.clone(), creator_token.clone(), bet_ai.clone(), token.clone()], &[seeds])?;
        invoke_signed(&ix_close(vault.key, creator.key, bet_ai.key),
                      &[vault.clone(), creator.clone(), bet_ai.clone(), token.clone()], &[seeds])?;
        let all = bet_ai.lamports();
        move_lamports(bet_ai, creator, all)?;
    }
    bet_ai.try_borrow_mut_data()?.fill(0);
    msg!("callit: bet {} cancelled", bet.bet_id);
    Ok(())
}

fn move_lamports(from: &AccountInfo, to: &AccountInfo, amount: u64) -> ProgramResult {
    if amount == 0 {
        return Ok(());
    }
    let mut f = from.try_borrow_mut_lamports()?;
    let mut t = to.try_borrow_mut_lamports()?;
    **f = f.checked_sub(amount).ok_or(ProgramError::InsufficientFunds)?;
    **t = t.checked_add(amount).ok_or(ProgramError::ArithmeticOverflow)?;
    Ok(())
}
