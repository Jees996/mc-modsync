package com.modsync.updater;

import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.components.Button;
import net.minecraft.client.gui.screens.TitleScreen;
import net.minecraft.client.gui.screens.multiplayer.JoinMultiplayerScreen;
import net.minecraft.network.chat.Component;
import net.minecraftforge.client.event.ScreenEvent;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.fml.common.Mod;

@Mod("modsync")
public final class UpdaterMod {
    public UpdaterMod() {
        MinecraftForge.EVENT_BUS.addListener(UpdaterMod::onScreenInit);
    }

    private static void onScreenInit(ScreenEvent.Init.Post event) {
        if (event.getScreen() instanceof TitleScreen screen) {
            event.addListener(Button.m_253074_(Component.m_237113_("整合包更新"), button ->
                    Minecraft.m_91087_().m_91152_(new UpdaterScreen(screen)))
                    .m_252987_(Math.max(4, screen.f_96543_ - 110), Math.max(4, screen.f_96544_ - 48), 104, 20)
                    .m_253136_());
        } else if (event.getScreen() instanceof JoinMultiplayerScreen screen) {
            event.addListener(Button.m_253074_(Component.m_237113_("§e§l【更新提示】§f进服提示模组版本不对？点此检查更新"), button ->
                    Minecraft.m_91087_().m_91152_(new UpdaterScreen(screen)))
                    .m_252987_(Math.max(4, screen.f_96543_ / 2 - 180), 8, 360, 20)
                    .m_253136_());
        }
    }
}
