import asyncio
import itertools
import logging
import math
import random
import string
from operator import itemgetter

import discord
from cogs.utils.checkers import is_quiz_channel
from discord.ext.commands import Cog, command, guild_only
from emoji import emojize
from sqlalchemy import func, insert, select

from pyboss.models import QuestionModel
from pyboss.utils import database
from pyboss.wrappers.guild import GuildWrapper
from pyboss.wrappers.member import MemberWrapper

logger = logging.getLogger(__name__)


class Question:
    """
    Represents a single question with attributes like related message
    and the winners or losers
    """

    COLOURS = [0xFFFF00, 0x0000FF, 0xFF0000, 0xFF75FF, 0x00FF00, 0x757575, 0x75FF75]

    TIMEOUT_MESSAGES = [
        "Le temps est écoulé!",
        "ding ding it's finish",
        "Il est l'heure !",
        "Allez hop on rembale !",
        "Coulé est le temps",
        "Le dernier grain de sable est tombé dans le sablier",
        "Avez vous vu l'heure !? Temps terminé !",
    ]

    def __init__(self, bot, channel: discord.TextChannel, question: QuestionModel):
        self.bot = bot
        self.channel = channel
        self.guild = GuildWrapper(channel.guild)
        self.winners = []  # We needs a list to preserve insertion order
        self.losers = []
        self.message = None
        # Get attributes from the question fetched
        self.author: str = question.author
        self.theme: str = question.theme
        self.title: str = question.title
        self.propositions: str = question.propositions
        self.answer: str = question.answer

    async def send(self):
        """
        Send a question in Quiz channel

        Question is a dict fetched from the quiz table that's contains field:
        author - theme - question - propositions - response
        """
        embed = discord.Embed(
            title=self.title,
            colour=random.choice(self.COLOURS),
            description=self.propositions,
        )
        embed.set_author(name=self.theme)
        embed.set_footer(text=f"Auteur: {self.author}")
        self.message = await self.channel.send(embed=embed)

        for line in self.propositions.split("\n"):
            # line is of the form "A) This is a proposition"
            if line:
                # Get emoji from it name that's depends on the first letter
                char = line[0].lower()
                emoji = emojize(f":regional_indicator:{char}", use_aliases=True)
                await self.message.add_reaction(emoji)

    async def send_rank(self) -> tuple[list, list]:
        """
        Check reactions, send a rank info and ajust XP of the members
        """

        def win_score(n, coef, level):
            return math.ceil(((200 + level) * math.sqrt(n)) / (math.sqrt(coef) * level))

        def lose_score(level):
            return math.ceil(20 * math.sqrt(level))

        description = "**Gagnants**: \n" if self.losers else ""
        nb_players = len(self.winners) + len(self.winners)

        for id, i in zip(self.winners, itertools.count(1)):
            member = self.guild.get_member(id)
            score = win_score(nb_players, i, member.level)
            description += f"{i}. {member.name}: +{score}XP \n"
            member.XP += score

        description += "\n**Perdants**: \n" if self.losers else ""
        for id in self.losers:
            member = self.guild.get_member(id)
            score = lose_score(nb_players)
            description += f":small_red_triangle_down: {member.name}: -{score}XP \n"
            member.XP -= score

        embed = discord.Embed(
            title=":hourglass: Résultats de la question:",
            colour=random.choice(self.COLOURS),
            description=description,
        )
        embed.set_footer(text=random.choice(self.TIMEOUT_MESSAGES))
        await self.channel.send(embed=embed)

        return self.winners, self.losers


class Quiz(Cog):
    """
    Quiz can permit to obtain XP and level up...
    """

    def __init__(self, bot):
        self.bot = bot
        self.actives = {}
        self.scores = {}

    @Cog.listener()
    @guild_only()
    async def on_message(self, msg: discord.Message):
        """Obtain a few XP per message"""
        if msg.author.id != self.bot.user.id and not msg.content.startswith("!"):
            try:
                mod_member = MemberWrapper(msg.author)
                mod_member.XP += 25
            except AttributeError:
                pass

    @Cog.listener("on_reaction_add")
    @guild_only()
    @is_quiz_channel()
    async def _reaction_on_question(self, reaction, player: discord.User):
        """
        Remove ex reactions of the user in a quiz question
        """
        msg = reaction.message
        question = self.actives.get(msg.channel.id)
        if not question or reaction.count <= 1:
            return

        char = question.answer.lower()
        emoji_answer = emojize(f"regional_indicator_{char}", use_aliases=True)

        for react in msg.reactions:
            async for user in react.users():
                if react is reaction and user is player:  # Player's current reaction
                    if react.emoji == emoji_answer:
                        question.player_wins.append(user.id)
                        question.player_loses.remove(user.id)
                    else:
                        question.player_loses.append(user.id)
                        question.player_wins.remove(user.id)
                elif user is player:  # Removes previous user reaction
                    await react.remove(user)

    @command(name="questions", aliases=["q", "question", "quiz"])
    @guild_only()
    @is_quiz_channel()
    async def questions(self, ctx, nb_questions: int = 1):
        """
        Génère n questions ou une seuel si aucun argument n'est donné.
        """

        def fetch_questions(number: int = 1):
            rand = func.random if database.get_dialect() == "mysql" else func.rand
            return database.execute(select(Question).order_by(rand()).limit(number))

        if self.actives.get(ctx.channel.id):
            return
        self.scores.clear()

        for question_model in fetch_questions(nb_questions):
            question = Question(self.bot, ctx.channel, question_model)
            await question.send()
            self.actives[ctx.channel.id] = question
            await asyncio.sleep(30.0)
            await question.send_rank()
            del self.actives[ctx.channel.id]

            winners, _ = await question.send_rank()
            for id in winners:
                member = GuildWrapper(ctx.guild).get_member_by_id(id)
                self.scores[member.name] = self.scores.get(member.name, 0) + 1

        await self.get_rank(ctx)

    @command(name="rank")
    @guild_only()
    @is_quiz_channel()
    async def get_rank(self, ctx):
        """
        Affiche le classement de la partie en cours
        """
        if not self.actives.get(ctx.channel.id):
            return
        titre, description = "Classements du Quiz:", ""
        classement = sorted(self.scores.items(), key=itemgetter(1), reverse=True)
        medals = [":first_place:", ":second_place:", ":third_place:"]

        for rang, (player, score) in enumerate(classement):
            rang = medals[rang] if rang < 3 else rang + 1
            description += f"{rang}  {player} : {score} points \n"

        await ctx.send(
            embed=discord.Embed(title=titre, colour=0x00FF00, description=description)
        )

    @command(name="question_add", aliases=["q_add"])
    @is_quiz_channel()
    async def question_add_procedure(self, ctx):
        """
        Ajouter une question dans la base de donnée (procedure)
        """
        await ctx.message.delete()

        async def send_question(q: str, timeout=60.0) -> str:
            msg = await ctx.send(q)
            msg_response = await self.bot.wait_for(
                "message", check=lambda m: m.author == ctx.author, timeout=timeout
            )
            content = msg_response.content
            await msg.delete()
            await msg_response.delete()
            return content

        try:
            theme = await send_question(
                "Quel est le thème de votre question ? (Ex: Informatique)", timeout=30.0
            )
            question = await send_question("Quelle est votre question ?", timeout=60.0)
            propositions = await send_question(
                "Quelles sont les propositions (séparées par des /, minimum 3) ? \n"
                "Mettez un * à la fin de la bonne proposition (Ex: P1* / P2 / P3)",
                timeout=180.0,
            )
        except asyncio.TimeoutError:
            await ctx.author.send(
                "Vous avez mis trop de temps à ajouter la question, veuillez réesayer"
            )
        else:
            propositions = propositions.split("/")
            response = None
            for (i, p), letter in zip(enumerate(propositions), string.ascii_uppercase):
                if p.strip().endswith("*"):
                    response = letter
                    p = p.rstrip("* ")
                propositions[i] = f"{letter}) {p}"

            if not response:
                logger.error(f"The question {question} hasn't response or propositions")
            propositions = "\n".join(propositions)
            database.execute(
                insert(Question).values(
                    author=ctx.author.name,
                    theme=theme,
                    question=question,
                    propositions=propositions,
                    answer=response,
                )
            )
            mod_member = MemberWrapper(ctx.author)
            mod_member.XP += 500
            embed = discord.Embed(
                title="Merci!",
                colour=0x5A546C,
                description=f"{ctx.author.mention} a ajouté une nouvelle question!",
            )
            embed.set_thumbnail(url=ctx.author.avatar_url)
            embed.set_author(name=ctx.author.name)
            await ctx.send(embed=embed)


def setup(bot):
    bot.add_cog(Quiz(bot))